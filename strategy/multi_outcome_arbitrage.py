"""多 outcome 结构套利检测。

覆盖的结构性机会（基于订单簿真实深度，非仅 mid 价）：

1. 单市场补集套利 (complement)：
   同一二元市场 YES_ask + NO_ask < 1 → 同时买 YES+NO，结算必得 $1。
   每套利润 = 1 - (yes_ask + no_ask)。

2. 互斥多结果「买完整集」(buy_set, 仅 neg-risk 事件)：
   一个 event 下 N 个互斥结果，恰有一个结算为 YES。
   若 sum(各 YES best_ask) < 1 → 各买 1 张 YES，结算必得 $1。
   每套利润 = 1 - sum(yes_ask)。

3. 互斥多结果「买完整 NO 集 / 反向」(sell_set, 仅 neg-risk 事件)：
   各买 1 张 NO，N 个里恰有 N-1 个结算为 YES(NO 那侧得 $1)，payout = N-1。
   每套利润 = (N-1) - sum(no_ask) = sum(yes_bid) - 1。
   等价于「YES 价格总和 > 1 时反向做 No」。

所有机会都做：深度感知定价、滑点估计、手续费扣除、净 edge 阈值过滤。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from config import Config
from models import Event, Market, OrderBook, TradeLeg, TradePlan

logger = logging.getLogger("strategy.arb")

BookGetter = Callable[[str], Optional[OrderBook]]


@dataclass
class _NoLevelVar:
    market: Market
    token_id: str
    price: float
    size: float


class MultiOutcomeArbitrage:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def scan_event(self, event: Event, get_book: BookGetter) -> List[TradePlan]:
        plans: List[TradePlan] = []

        # Optional legacy strategy: a single binary market YES+NO complement.
        if self.cfg.enable_complement_strategy:
            for market in event.markets:
                plan = self._complement(event, market, get_book)
                if plan:
                    plans.append(plan)

        # 策略 2 & 3：仅对 neg-risk（互斥多结果）事件
        if event.neg_risk and len(event.markets) >= 2:
            if self.cfg.enable_yes_complete_set:
                buy_set = self._buy_yes_set(event, get_book)
                if buy_set:
                    plans.append(buy_set)
            if self.cfg.enable_equal_no_basket:
                sell_set = self._buy_no_set(event, get_book)
                if sell_set:
                    plans.append(sell_set)
            if self.cfg.enable_unequal_no_basket:
                unequal_no = self._unequal_no_basket(event, get_book)
                if unequal_no:
                    plans.append(unequal_no)

        return plans

    # ---------------- 策略 1：补集 ----------------
    def _complement(self, event: Event, market: Market, get_book: BookGetter) -> Optional[TradePlan]:
        yes_tid, no_tid = market.yes_token_id, market.no_token_id
        if not yes_tid or not no_tid:
            return None
        yes_book, no_book = get_book(yes_tid), get_book(no_tid)
        if not yes_book or not no_book:
            return None

        size = self._feasible_buy_size([yes_book, no_book])
        if size <= 0:
            return None

        yes_fill = yes_book.cost_to_buy(size)
        no_fill = no_book.cost_to_buy(size)
        if not yes_fill or not no_fill:
            return None
        yes_avg, yes_filled = yes_fill
        no_avg, no_filled = no_fill
        filled = min(yes_filled, no_filled)
        if filled <= 0:
            return None

        cost_per_set = yes_avg + no_avg
        edge_per_unit = 1.0 - cost_per_set
        top_cost = (yes_book.best_ask.price + no_book.best_ask.price)
        slippage = max(0.0, cost_per_set - top_cost)

        plan = self._build_plan(
            strategy="complement",
            event=event,
            legs_spec=[
                (market, yes_tid, "Yes", yes_avg),
                (market, no_tid, "No", no_avg),
            ],
            size=filled,
            edge_per_unit=edge_per_unit,
            payout_per_set=1.0,
            slippage=slippage,
            depths=[yes_filled, no_filled],
            notes="同一市场 YES+NO 低于面值，买入补集锁定 $1",
        )
        return plan if self._passes(plan) else None

    # ---------------- 策略 2：买 YES 完整集 ----------------
    def _buy_yes_set(self, event: Event, get_book: BookGetter) -> Optional[TradePlan]:
        legs_data = []
        books = []
        for m in event.markets:
            tid = m.yes_token_id
            book = get_book(tid) if tid else None
            if not tid or not book or not book.best_ask:
                return None  # 任一腿缺簿则放弃该组合
            legs_data.append((m, tid))
            books.append(book)

        size = self._feasible_buy_size(books)
        if size <= 0:
            return None

        avgs, depths, top_sum, avg_sum = [], [], 0.0, 0.0
        for book in books:
            fill = book.cost_to_buy(size)
            if not fill:
                return None
            avg, filled = fill
            avgs.append(avg)
            depths.append(filled)
            avg_sum += avg
            top_sum += book.best_ask.price
        filled_size = min(depths)
        edge_per_unit = 1.0 - avg_sum
        slippage = max(0.0, avg_sum - top_sum)

        plan = self._build_plan(
            strategy="buy_set",
            event=event,
            legs_spec=[(m, tid, m.group_item_title or "Yes", avg)
                       for (m, tid), avg in zip(legs_data, avgs)],
            size=filled_size,
            edge_per_unit=edge_per_unit,
            payout_per_set=1.0,
            slippage=slippage,
            depths=depths,
            notes=f"互斥 {len(legs_data)} 结果 YES 总和={avg_sum:.4f}<1，买完整集锁定 $1",
        )
        return plan if self._passes(plan) else None

    # ---------------- 策略 3：买 NO 完整集（反向）----------------
    def _buy_no_set(self, event: Event, get_book: BookGetter) -> Optional[TradePlan]:
        n = len(event.markets)
        legs_data, books = [], []
        for m in event.markets:
            tid = m.no_token_id
            book = get_book(tid) if tid else None
            if not tid or not book or not book.best_ask:
                return None
            legs_data.append((m, tid))
            books.append(book)

        size = self._feasible_buy_size(books)
        if size <= 0:
            return None

        avgs, depths, top_sum, avg_sum = [], [], 0.0, 0.0
        for book in books:
            fill = book.cost_to_buy(size)
            if not fill:
                return None
            avg, filled = fill
            avgs.append(avg)
            depths.append(filled)
            avg_sum += avg
            top_sum += book.best_ask.price
        filled_size = min(depths)
        # 买 N 个 NO，payout = N-1
        payout_per_set = float(n - 1)
        edge_per_unit = payout_per_set - avg_sum
        slippage = max(0.0, avg_sum - top_sum)

        plan = self._build_plan(
            strategy="sell_set",
            event=event,
            legs_spec=[(m, tid, f"No:{m.group_item_title or m.question[:16]}", avg)
                       for (m, tid), avg in zip(legs_data, avgs)],
            size=filled_size,
            edge_per_unit=edge_per_unit,
            payout_per_set=payout_per_set,
            slippage=slippage,
            depths=depths,
            notes=f"互斥 {n} 结果 YES 总和>1，买完整 NO 集，payout={n-1}",
        )
        return plan if self._passes(plan) else None

    # ---------------- 策略 3b：不等额 NO Basket ----------------
    def _unequal_no_basket(self, event: Event, get_book: BookGetter) -> Optional[TradePlan]:
        """线性规划寻找不等额 NO basket。

        PDF 里的公式是：
            profit = (sum(q_i) - max(q_i)) - sum(cost_i)

        这里把每个 NO ask 档位作为一个可买变量，并增加 W 变量表示
        worst_payout。约束 W <= sum(q_j for j != i)，目标最大化
        W - total_cost。每个 outcome 的最大买入份额用 DEFAULT_ORDER_SIZE
        截断，避免优化器在深盘口上给出超出当前试运行规模的组合。
        """
        try:
            from scipy.optimize import linprog
        except Exception:
            logger.debug("scipy 未安装，跳过 unequal_no_basket")
            return None

        if len(event.markets) < 3:
            return None

        max_per_outcome = max(0.0, self.cfg.default_order_size)
        if max_per_outcome <= 0:
            return None

        vars_: List[_NoLevelVar] = []
        outcome_var_indexes: List[List[int]] = []
        top_prices: Dict[str, float] = {}

        for market in event.markets:
            token_id = market.no_token_id
            book = get_book(token_id) if token_id else None
            if not token_id or not book or not book.best_ask:
                return None

            top_prices[token_id] = book.best_ask.price
            indexes: List[int] = []
            remaining_cap = max_per_outcome
            for level in sorted(book.asks, key=lambda lvl: lvl.price):
                if remaining_cap <= 1e-9:
                    break
                size = min(level.size, remaining_cap)
                if size <= 1e-9:
                    continue
                indexes.append(len(vars_))
                vars_.append(_NoLevelVar(market, token_id, level.price, size))
                remaining_cap -= size
            if not indexes:
                return None
            outcome_var_indexes.append(indexes)

        var_count = len(vars_)
        w_index = var_count

        # linprog does minimization, so minimize total_cost - W.
        objective = [v.price for v in vars_] + [-1.0]
        bounds = [(0.0, v.size) for v in vars_] + [(0.0, None)]

        constraints = []
        rhs = []
        for indexes in outcome_var_indexes:
            row = [0.0] * (var_count + 1)
            excluded = set(indexes)
            for j in range(var_count):
                if j not in excluded:
                    row[j] = -1.0
            row[w_index] = 1.0
            constraints.append(row)
            rhs.append(0.0)

        result = linprog(
            c=objective,
            A_ub=constraints,
            b_ub=rhs,
            bounds=bounds,
            method="highs",
        )
        if not result.success:
            logger.debug(
                "unequal_no_basket optimizer failed event=%s: %s",
                event.event_id,
                result.message,
            )
            return None

        quantities = [max(0.0, float(x)) for x in result.x[:var_count]]
        worst_payout = max(0.0, float(result.x[w_index]))

        by_token: Dict[str, dict] = {}
        for qty, var in zip(quantities, vars_):
            if qty <= 1e-6:
                continue
            row = by_token.setdefault(
                var.token_id,
                {"market": var.market, "qty": 0.0, "cost": 0.0, "top_cost": 0.0},
            )
            row["qty"] += qty
            row["cost"] += qty * var.price
            row["top_cost"] += qty * top_prices[var.token_id]

        if len(by_token) < 2:
            return None

        total_cost = sum(row["cost"] for row in by_token.values())
        fee_cost = self.cfg.fee_rate * total_cost
        profit = worst_payout - total_cost - fee_cost
        if total_cost <= 0 or worst_payout <= 0 or profit <= 0:
            return None

        roi = profit / total_cost
        total_shares = sum(row["qty"] for row in by_token.values())
        top_cost = sum(row["top_cost"] for row in by_token.values())
        slippage = max(0.0, (total_cost - top_cost) / total_shares) if total_shares > 0 else 0.0

        legs: List[TradeLeg] = []
        depths: List[float] = []
        for token_id, row in by_token.items():
            market = row["market"]
            qty = row["qty"]
            avg_price = row["cost"] / qty
            depths.append(qty)
            legs.append(TradeLeg(
                event_id=event.event_id,
                market_id=market.market_id,
                market_question=market.question,
                token_id=token_id,
                outcome=f"No:{market.group_item_title or market.question[:16]}",
                side="BUY",
                price=round(avg_price, 4),
                size=round(qty, 2),
                available_depth=round(qty, 2),
            ))

        plan = TradePlan(
            strategy="unequal_no_basket",
            event_id=event.event_id,
            event_title=event.title,
            legs=legs,
            est_cost=round(total_cost, 4),
            est_max_payout=round(worst_payout, 4),
            est_profit=round(profit, 4),
            edge=round(roi - self.cfg.slippage_buffer, 4),
            slippage=round(slippage, 4),
            fee_cost=round(fee_cost, 4),
            min_depth=round(min(depths), 2) if depths else 0.0,
            notes=(
                f"Unequal NO basket: worst_payout={worst_payout:.4f}, "
                f"cost={total_cost:.4f}, profit={profit:.4f}, roi={roi*100:.3f}%"
            ),
        )
        return plan if self._passes(plan) else None

    # ---------------- 通用辅助 ----------------
    def _feasible_buy_size(self, books: List[OrderBook]) -> float:
        """各腿 ask 总深度的最小值，再按配置上限截断。"""
        target = self.cfg.default_order_size
        min_depth = target
        for book in books:
            depth = sum(l.size for l in book.asks)
            min_depth = min(min_depth, depth)
        return max(0.0, min(target, min_depth))

    def _build_plan(
        self, strategy: str, event: Event, legs_spec, size: float,
        edge_per_unit: float, payout_per_set: float, slippage: float,
        depths: List[float], notes: str,
    ) -> TradePlan:
        legs: List[TradeLeg] = []
        for (market, token_id, outcome, price), depth in zip(legs_spec, depths):
            legs.append(TradeLeg(
                event_id=event.event_id,
                market_id=market.market_id,
                market_question=market.question,
                token_id=token_id,
                outcome=outcome,
                side="BUY",
                price=round(price, 4),
                size=round(size, 2),
                available_depth=round(depth, 2),
            ))
        est_cost = sum(leg.price * leg.size for leg in legs)
        est_max_payout = payout_per_set * size
        fee_cost = self.cfg.fee_rate * est_cost
        est_profit = edge_per_unit * size - fee_cost
        # 扣除滑点缓冲后的净 edge（每套）
        net_edge = edge_per_unit - self.cfg.slippage_buffer
        return TradePlan(
            strategy=strategy,
            event_id=event.event_id,
            event_title=event.title,
            legs=legs,
            est_cost=round(est_cost, 4),
            est_max_payout=round(est_max_payout, 4),
            est_profit=round(est_profit, 4),
            edge=round(net_edge, 4),
            slippage=round(slippage, 4),
            fee_cost=round(fee_cost, 4),
            min_depth=round(min(depths), 2) if depths else 0.0,
            notes=notes,
        )

    def _passes(self, plan: TradePlan) -> bool:
        """策略层初筛：净 edge 与滑点阈值。风控层会再次严格校验。"""
        if plan.edge < self.cfg.min_edge:
            return False
        if plan.slippage > self.cfg.risk_max_slippage:
            return False
        if plan.min_depth <= 0:
            return False
        return True
