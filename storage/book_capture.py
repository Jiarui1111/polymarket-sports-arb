"""发现机会时采集订单簿：Top N 档位 + WS 最近 tick 历史，并估算深度成交利润。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from models import OrderBook, PriceLevel, TradePlan

if TYPE_CHECKING:
    from ws.orderbook_ws import OrderBookCache, TickHistory


@dataclass
class BookLevelRow:
    token_id: str
    side: str  # bid | ask
    level_rank: int
    price: float
    size: float


@dataclass
class BookTickRow:
    token_id: str
    side: str
    tick_seq: int
    event_type: str
    price: float
    size: float
    ws_ts: float


@dataclass
class TokenBookCapture:
    token_id: str
    book_ts: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    levels: List[BookLevelRow] = field(default_factory=list)
    ticks: List[BookTickRow] = field(default_factory=list)


@dataclass
class OpportunityBookContext:
    plan: TradePlan
    tokens: Dict[str, TokenBookCapture] = field(default_factory=dict)
    sim_fill_cost: Optional[float] = None
    sim_fill_profit: Optional[float] = None
    sim_fill_size: Optional[float] = None


def capture_opportunity_books(
    cache: "OrderBookCache",
    tick_history: "TickHistory",
    plan: TradePlan,
    *,
    level_depth: int = 5,
    tick_depth: int = 5,
) -> OpportunityBookContext:
    """在机会触发瞬间复制订单簿与 tick 队列（线程安全读）。"""
    token_ids = list(dict.fromkeys(leg.token_id for leg in plan.legs))
    ctx = OpportunityBookContext(plan=plan)
    target_size = plan.legs[0].size if plan.legs else 0.0

    for token_id in token_ids:
        book = cache.get_copy(token_id)
        cap = TokenBookCapture(token_id=token_id)
        if book:
            cap.book_ts = book.ts
            if book.best_bid:
                cap.best_bid = book.best_bid.price
            if book.best_ask:
                cap.best_ask = book.best_ask.price
            cap.levels = _top_levels(book, level_depth)
        tick_map = tick_history.get_ticks(token_id, tick_depth)
        for side in ("bid", "ask"):
            for seq, t in enumerate(tick_map.get(side, []), start=1):
                cap.ticks.append(
                    BookTickRow(t.token_id, side, seq, t.event_type, t.price, t.size, t.ts)
                )
        ctx.tokens[token_id] = cap

    if plan.strategy == "unequal_no_basket":
        cost, payout, shares = _simulate_planned_legs(plan, ctx)
        ctx.sim_fill_cost = cost
        ctx.sim_fill_size = shares
        if cost is not None and payout is not None:
            ctx.sim_fill_profit = payout - cost
    elif target_size > 0:
        ctx.sim_fill_cost, ctx.sim_fill_size = _simulate_buy_all_legs(plan, ctx, target_size)
        if ctx.sim_fill_cost is not None and ctx.sim_fill_size and ctx.sim_fill_size > 0:
            payout = _payout_for_plan(plan, ctx.sim_fill_size)
            ctx.sim_fill_profit = payout - ctx.sim_fill_cost
    return ctx


def _top_levels(book: OrderBook, depth: int) -> List[BookLevelRow]:
    rows: List[BookLevelRow] = []
    for i, lvl in enumerate(book.bids[:depth], start=1):
        rows.append(BookLevelRow(book.token_id, "bid", i, lvl.price, lvl.size))
    for i, lvl in enumerate(book.asks[:depth], start=1):
        rows.append(BookLevelRow(book.token_id, "ask", i, lvl.price, lvl.size))
    return rows


def _simulate_buy_all_legs(
    plan: TradePlan, ctx: OpportunityBookContext, target_size: float
) -> tuple[Optional[float], Optional[float]]:
    """按各腿 ask 深度吃单；成套数量 = 各腿可成交量的最小值。"""
    min_filled = target_size
    for leg in plan.legs:
        cap = ctx.tokens.get(leg.token_id)
        if not cap:
            return None, None
        fill = _levels_to_book(cap).cost_to_buy(target_size)
        if not fill:
            return None, None
        _, filled = fill
        min_filled = min(min_filled, filled)
    if min_filled <= 0:
        return None, None
    total_cost = 0.0
    for leg in plan.legs:
        cap = ctx.tokens[leg.token_id]
        fill = _levels_to_book(cap).cost_to_buy(min_filled)
        if not fill:
            return None, None
        avg, _ = fill
        total_cost += avg * min_filled
    return total_cost, min_filled


def _simulate_planned_legs(
    plan: TradePlan, ctx: OpportunityBookContext
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """按每条腿自己的计划 size 复算成本，并计算 unequal NO basket worst payout。"""
    total_cost = 0.0
    sizes: List[float] = []
    for leg in plan.legs:
        cap = ctx.tokens.get(leg.token_id)
        if not cap or leg.size <= 0:
            return None, None, None
        fill = _levels_to_book(cap).cost_to_buy(leg.size)
        if not fill:
            return None, None, None
        avg, filled = fill
        if filled + 1e-9 < leg.size:
            return None, None, None
        total_cost += avg * leg.size
        sizes.append(leg.size)

    total_shares = sum(sizes)
    if plan.strategy == "unequal_no_basket":
        payout = total_shares - max(sizes) if sizes else 0.0
    else:
        payout = plan.est_max_payout
    return total_cost, payout, total_shares


def _payout_for_plan(plan: TradePlan, size: float) -> float:
    if plan.strategy == "complement":
        return size * 1.0
    if plan.strategy == "buy_set":
        return size * 1.0
    if plan.strategy == "sell_set":
        n = len(plan.legs)
        return size * float(max(0, n - 1))
    if plan.strategy == "unequal_no_basket":
        sizes = [leg.size for leg in plan.legs]
        return sum(sizes) - max(sizes) if sizes else 0.0
    return plan.est_max_payout


def _levels_to_book(cap: TokenBookCapture) -> OrderBook:
    bids = [PriceLevel(r.price, r.size) for r in cap.levels if r.side == "bid"]
    asks = [PriceLevel(r.price, r.size) for r in cap.levels if r.side == "ask"]
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBook(token_id=cap.token_id, bids=bids, asks=asks, ts=cap.book_ts or time.time())
