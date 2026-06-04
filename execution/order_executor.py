"""Order execution for detected arbitrage plans.

Dry-run mode keeps writing opportunities before simulated orders so local
analysis remains convenient. Real mode prioritizes execution: after risk checks
pass, orders are sent first and database writes happen after the execution
attempt finishes.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

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
        """Handle one opportunity from strategy output."""
        for line in plan.summary_lines():
            logger.info(line)
        self._log_opportunity_depth(plan, book_ctx)

        decision = self.risk.evaluate(plan)
        if not decision.approved:
            logger.info("[SKIP] risk rejected: %s", "; ".join(decision.reasons))
            if not self.cfg.is_real:
                self._save_opportunity(plan, book_ctx)
            return

        if self.cfg.is_real:
            if not self.clob.trade_ready:
                logger.warning("[REAL] trading client not ready; falling back to simulation")
                opportunity_id = self._save_opportunity(plan, book_ctx)
                self._simulate(plan, opportunity_id)
                return

            results = self._execute_real(plan)
            opportunity_id = self._save_opportunity(plan, book_ctx)
            self._save_order_results("real", plan, results, opportunity_id)
            return

        opportunity_id = self._save_opportunity(plan, book_ctx)
        self._simulate(plan, opportunity_id)

    def _save_opportunity(
        self, plan: TradePlan, book_ctx: Optional[OpportunityBookContext]
    ) -> Optional[int]:
        opportunity_id: Optional[int] = None
        try:
            opportunity_id = self.db.save_opportunity(plan, book_ctx)
            if book_ctx and book_ctx.sim_fill_profit is not None:
                logger.info(
                    "  [DEPTH_SIM] cost=%.4f profit=%.4f fill_size=%.2f",
                    book_ctx.sim_fill_cost or 0.0,
                    book_ctx.sim_fill_profit,
                    book_ctx.sim_fill_size or 0.0,
                )
        except Exception as exc:
            logger.exception("failed to save opportunity: %s", exc)
        return opportunity_id

    def _save_order_results(
        self,
        mode: str,
        plan: TradePlan,
        results: List[Tuple[int, OrderResult]],
        opportunity_id: Optional[int],
    ) -> None:
        for leg_index, result in results:
            try:
                self.db.save_order(mode, plan, leg_index, result, opportunity_id=opportunity_id)
            except Exception as exc:
                logger.exception(
                    "failed to save order result leg=%d order_id=%s: %s",
                    leg_index,
                    result.order_id,
                    exc,
                )

    def _log_opportunity_depth(
        self, plan: TradePlan, book_ctx: Optional[OpportunityBookContext]
    ) -> None:
        """Log market, plan size, and orderbook depth at trigger time."""
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
        logger.info("[DRY-RUN] simulate %d legs; no real orders sent", len(plan.legs))
        for i, leg in enumerate(plan.legs):
            result = OrderResult(
                success=True,
                order_id=f"SIM-{int(time.time() * 1000)}-{i}",
                status="simulated",
                filled_size=leg.size,
            )
            self.db.save_order("dry_run", plan, i, result, opportunity_id=opportunity_id)
            logger.info("  [SIM] %s %.1f @ %.4f (%s)", leg.side, leg.size, leg.price, leg.outcome)

    # ---------------- real ----------------
    def _execute_real(self, plan: TradePlan) -> List[Tuple[int, OrderResult]]:
        logger.warning("[REAL] sending orders now: %s / %s", plan.strategy, plan.event_title)
        self.risk.register(plan)
        placed: List[Tuple[int, OrderResult]] = []
        all_ok = True

        try:
            for i, leg in enumerate(plan.legs):
                result = self.clob.place_limit_order(
                    token_id=leg.token_id,
                    side=leg.side,
                    price=leg.price,
                    size=leg.size,
                    order_type="FOK",
                )
                placed.append((i, result))

                if not result.success:
                    logger.error("  leg %d order failed: %s; stopping", i, result.error)
                    all_ok = False
                    break

                confirmed = self._confirm_fill(result)
                if not confirmed:
                    all_ok = False
                    logger.error(
                        "  leg %d not filled within %.1fs; order cancelled; stopping",
                        i,
                        self.cfg.order_timeout_sec,
                    )
                    break
                logger.info("  leg %d fill confirmed: %s", i, result.order_id)

        except Exception as exc:
            all_ok = False
            logger.exception("real execution failed: %s", exc)
        finally:
            if not all_ok:
                self.risk.release(plan)
                logger.warning(
                    "[REAL] plan did not fully fill; exposure released. Check any partial leg manually."
                )
            else:
                logger.warning("[REAL] all legs filled: %s", plan.event_title)
        return placed

    def _confirm_fill(self, result: OrderResult) -> bool:
        """Confirm fill status. REST here is for order status, not market data."""
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

        self.clob.cancel_order(result.order_id)
        return False


def _short_token(token_id: str) -> str:
    if len(token_id) <= 14:
        return token_id
    return f"{token_id[:10]}...{token_id[-4:]}"


def _fmt_price(value: Optional[float]) -> str:
    return "none" if value is None else f"{value:.4f}"
