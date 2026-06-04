"""订单执行器。

两种模式：
- dry_run（默认）：只打印交易计划并落库，绝不真实下单。
- real：经风控批准后真实下单。

执行细节（real）：
- 每条腿用可成交限价单提交（默认 FOK，尽量原子化，减少单腿成交带来的方向暴露）。
- 提交后轮询订单状态确认成交；超时未成交自动撤单。
- 任一腿失败则停止后续腿，记录告警（剩余敞口需人工/后续处理），并释放风控暴露。
- 全程不打印私钥 / 凭证。
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from config import Config
from data.clob_client import ClobMarketClient
from models import OrderResult, TradePlan
from risk.risk_manager import RiskManager
from storage.book_capture import OpportunityBookContext
from storage.db import Database

logger = logging.getLogger("execution.executor")


class OrderExecutor:
    def __init__(self, cfg: Config, clob: ClobMarketClient, risk: RiskManager, db: Database) -> None:
        self.cfg = cfg
        self.clob = clob
        self.risk = risk
        self.db = db

    def handle_opportunity(
        self, plan: TradePlan, book_ctx: Optional[OpportunityBookContext] = None
    ) -> None:
        """统一入口：打印计划 -> 落库（含订单簿）-> 按模式执行。"""
        for line in plan.summary_lines():
            logger.info(line)
        self._log_opportunity_depth(plan, book_ctx)

        opportunity_id: Optional[int] = None
        try:
            opportunity_id = self.db.save_opportunity(plan, book_ctx)
            if book_ctx and book_ctx.sim_fill_profit is not None:
                logger.info(
                    "  [深度模拟] 成本=%.4f 利润=%.4f 可成套=%.2f 张",
                    book_ctx.sim_fill_cost or 0,
                    book_ctx.sim_fill_profit,
                    book_ctx.sim_fill_size or 0,
                )
        except Exception as exc:
            logger.exception("机会落库失败（继续执行/风控）: %s", exc)

        decision = self.risk.evaluate(plan)
        if not decision.approved:
            logger.info("[跳过] 风控未通过：%s", "; ".join(decision.reasons))
            return

        if not self.cfg.is_real:
            self._simulate(plan, opportunity_id)
            return

        if not self.clob.trade_ready:
            logger.warning("[real] 交易客户端未就绪，降级为模拟。请检查 .env 凭证。")
            self._simulate(plan, opportunity_id)
            return

        self._execute_real(plan, opportunity_id)

    def _log_opportunity_depth(
        self, plan: TradePlan, book_ctx: Optional[OpportunityBookContext]
    ) -> None:
        """本地运行时打印机会市场、计划 size 和触发瞬间盘口深度。"""
        logger.info(
            "[OPPORTUNITY_MARKET] strategy=%s event_id=%s market=%s legs=%d "
            "cost=%.4f payout=%.4f profit=%.4f edge=%.4f",
            plan.strategy,
            plan.event_id,
            plan.event_title,
            len(plan.legs),
            plan.est_cost,
            plan.est_max_payout,
            plan.est_profit,
            plan.edge,
        )
        if not book_ctx:
            logger.info("[OPPORTUNITY_DEPTH] no book context captured")
            return

        if book_ctx.sim_fill_profit is not None:
            logger.info(
                "[OPPORTUNITY_DEPTH] simulated_cost=%.4f simulated_profit=%.4f "
                "simulated_size=%.2f",
                book_ctx.sim_fill_cost or 0.0,
                book_ctx.sim_fill_profit,
                book_ctx.sim_fill_size or 0.0,
            )

        level_limit = max(1, self.cfg.book_level_depth)
        for i, leg in enumerate(plan.legs):
            cap = book_ctx.tokens.get(leg.token_id)
            if not cap:
                logger.info(
                    "[OPPORTUNITY_LEG] idx=%d side=%s outcome=%s token=%s "
                    "plan_size=%.2f plan_price=%.4f available_depth=%.2f book=missing",
                    i,
                    leg.side,
                    leg.outcome,
                    _short_token(leg.token_id),
                    leg.size,
                    leg.price,
                    leg.available_depth,
                )
                continue

            logger.info(
                "[OPPORTUNITY_LEG] idx=%d side=%s outcome=%s token=%s "
                "plan_size=%.2f plan_price=%.4f available_depth=%.2f "
                "best_bid=%s best_ask=%s book_age=%.2fs",
                i,
                leg.side,
                leg.outcome,
                _short_token(leg.token_id),
                leg.size,
                leg.price,
                leg.available_depth,
                _fmt_price(cap.best_bid),
                _fmt_price(cap.best_ask),
                max(0.0, time.time() - cap.book_ts) if cap.book_ts else -1.0,
            )
            for side in ("bid", "ask"):
                levels = [lvl for lvl in cap.levels if lvl.side == side][:level_limit]
                formatted = ", ".join(f"{lvl.price:.4f}x{lvl.size:.2f}" for lvl in levels)
                logger.info(
                    "[OPPORTUNITY_BOOK] idx=%d token=%s side=%s levels=%s",
                    i,
                    _short_token(leg.token_id),
                    side,
                    formatted or "(empty)",
                )

    # ---------------- dry-run ----------------
    def _simulate(self, plan: TradePlan, opportunity_id: Optional[int] = None) -> None:
        logger.info("[DRY-RUN] 模拟执行 %d 条腿，不发送真实订单", len(plan.legs))
        for i, leg in enumerate(plan.legs):
            result = OrderResult(
                success=True, order_id=f"SIM-{int(time.time()*1000)}-{i}",
                status="simulated", filled_size=leg.size,
            )
            self.db.save_order("dry_run", plan, i, result, opportunity_id=opportunity_id)
            logger.info("  [SIM] %s %.1f @ %.4f (%s)", leg.side, leg.size, leg.price, leg.outcome)

    # ---------------- real ----------------
    def _execute_real(self, plan: TradePlan, opportunity_id: Optional[int] = None) -> None:
        logger.warning("[REAL] 开始真实下单：%s / %s", plan.strategy, plan.event_title)
        self.risk.register(plan)
        placed: List[tuple[int, OrderResult]] = []
        all_ok = True

        try:
            for i, leg in enumerate(plan.legs):
                result = self.clob.place_limit_order(
                    token_id=leg.token_id, side=leg.side,
                    price=leg.price, size=leg.size, order_type="FOK",
                )
                self.db.save_order("real", plan, i, result, opportunity_id=opportunity_id)
                placed.append((i, result))

                if not result.success:
                    logger.error("  腿 %d 下单失败：%s，终止后续腿", i, result.error)
                    all_ok = False
                    break

                confirmed = self._confirm_fill(result)
                if not confirmed:
                    all_ok = False
                    logger.error("  腿 %d 未在 %ss 内成交，已撤单，终止", i, self.cfg.order_timeout_sec)
                    break
                logger.info("  腿 %d 成交确认：%s", i, result.order_id)

        except Exception as exc:
            all_ok = False
            logger.exception("真实下单过程中异常：%s", exc)
        finally:
            if not all_ok:
                self.risk.release(plan)
                logger.warning(
                    "[REAL] 套利未完整成交，已释放风控暴露。请人工检查是否有单腿敞口需平仓！"
                )
            else:
                logger.warning("[REAL] 套利各腿已成交：%s", plan.event_title)

    def _confirm_fill(self, result: OrderResult) -> bool:
        """确认订单成交。FOK 通常立即终态；否则轮询，超时撤单。

        生产环境可改为监听 user WebSocket；此处用 REST 轮询保证可靠性。
        """
        # FOK：若响应已是终态成交，直接通过
        status = (result.status or "").lower()
        if status in ("matched", "filled") or result.filled_size > 0:
            return True
        if not result.order_id:
            return False

        deadline = time.time() + self.cfg.order_timeout_sec
        while time.time() < deadline:
            info = self.clob.get_order(result.order_id)
            if info:
                st = str(info.get("status", "")).lower()
                size_matched = float(info.get("size_matched", 0) or 0)
                if st in ("matched", "filled") or size_matched > 0:
                    return True
                if st in ("canceled", "cancelled"):
                    return False
            time.sleep(1.0)

        # 超时：撤单
        self.clob.cancel_order(result.order_id)
        return False


def _short_token(token_id: str) -> str:
    if len(token_id) <= 14:
        return token_id
    return f"{token_id[:10]}...{token_id[-4:]}"


def _fmt_price(value: Optional[float]) -> str:
    return "none" if value is None else f"{value:.4f}"
