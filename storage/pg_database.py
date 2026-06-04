"""PostgreSQL 持久化：机会、订单簿快照、订单记录。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import psycopg2
import psycopg2.extras

from config import Config
from models import OrderResult, TradePlan
from storage.book_capture import OpportunityBookContext

logger = logging.getLogger("storage.pg")


class Database:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if not cfg.pg_dsn:
            raise ValueError("PostgreSQL 未配置：请设置 DATABASE_URL 或 PG_HOST/PG_USER/PG_PASSWORD/PG_DATABASE")
        self._lock = threading.Lock()
        self._test_connection()
        logger.info("PostgreSQL 已连接")

    def _test_connection(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    def _connect(self):
        return psycopg2.connect(self.cfg.pg_dsn)

    def close(self) -> None:
        pass

    def save_opportunity(
        self,
        plan: TradePlan,
        book_ctx: Optional[OpportunityBookContext] = None,
    ) -> int:
        """写入机会主表、腿、订单簿档位与 WS tick。返回 opportunity_id。"""
        plan_size = plan.legs[0].size if plan.legs else 0.0
        sim_cost = sim_profit = sim_size = None
        if book_ctx:
            sim_cost = book_ctx.sim_fill_cost
            sim_profit = book_ctx.sim_fill_profit
            sim_size = book_ctx.sim_fill_size

        with self._lock, self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO opportunities (
                        detected_at, strategy, event_id, event_title,
                        edge, est_cost, est_profit, est_max_payout,
                        slippage, fee_cost, min_depth, plan_size,
                        sim_fill_cost, sim_fill_profit, sim_fill_size, notes
                    ) VALUES (
                        to_timestamp(%s), %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) RETURNING id
                    """,
                    (
                        plan.ts, plan.strategy, plan.event_id, plan.event_title,
                        plan.edge, plan.est_cost, plan.est_profit, plan.est_max_payout,
                        plan.slippage, plan.fee_cost, plan.min_depth, plan_size,
                        sim_cost, sim_profit, sim_size, plan.notes,
                    ),
                )
                opp_id = int(cur.fetchone()[0])

                for i, leg in enumerate(plan.legs):
                    cur.execute(
                        """
                        INSERT INTO opportunity_legs (
                            opportunity_id, leg_index, market_id, market_question,
                            token_id, outcome, side, price, size, available_depth, notional
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            opp_id, i, leg.market_id, leg.market_question,
                            leg.token_id, leg.outcome, leg.side, leg.price, leg.size,
                            leg.available_depth, leg.notional,
                        ),
                    )

                if book_ctx:
                    self._save_book_context(cur, opp_id, book_ctx)
                self._increment_daily_opportunity_stats(cur, plan)

            conn.commit()
        logger.info(
            "机会已落库 id=%s strategy=%s sim_profit=%s",
            opp_id, plan.strategy, sim_profit,
        )
        return opp_id

    def _increment_daily_opportunity_stats(self, cur, plan: TradePlan) -> None:
        cur.execute(
            """
            INSERT INTO daily_opportunity_stats (
                stat_date, strategy, opportunity_count, total_est_profit,
                max_edge, first_detected_at, last_detected_at, updated_at
            ) VALUES (
                (to_timestamp(%s) AT TIME ZONE 'UTC')::date,
                %s,
                1,
                %s,
                %s,
                to_timestamp(%s),
                to_timestamp(%s),
                NOW()
            )
            ON CONFLICT (stat_date, strategy) DO UPDATE SET
                opportunity_count = daily_opportunity_stats.opportunity_count + 1,
                total_est_profit = daily_opportunity_stats.total_est_profit + EXCLUDED.total_est_profit,
                max_edge = GREATEST(daily_opportunity_stats.max_edge, EXCLUDED.max_edge),
                first_detected_at = LEAST(daily_opportunity_stats.first_detected_at, EXCLUDED.first_detected_at),
                last_detected_at = GREATEST(daily_opportunity_stats.last_detected_at, EXCLUDED.last_detected_at),
                updated_at = NOW()
            """,
            (
                plan.ts,
                plan.strategy,
                plan.est_profit,
                plan.edge,
                plan.ts,
                plan.ts,
            ),
        )

    def _save_book_context(self, cur, opp_id: int, ctx: OpportunityBookContext) -> None:
        level_rows = []
        tick_rows = []
        for token_id, cap in ctx.tokens.items():
            for lvl in cap.levels:
                level_rows.append((opp_id, lvl.token_id, lvl.side, lvl.level_rank, lvl.price, lvl.size))
            for tick in cap.ticks:
                tick_rows.append(
                    (
                        opp_id, tick.token_id, tick.side, tick.tick_seq,
                        tick.event_type, tick.price, tick.size, tick.ws_ts,
                    )
                )

        if level_rows:
            psycopg2.extras.execute_batch(
                cur,
                """
                INSERT INTO opportunity_book_levels (
                    opportunity_id, token_id, side, level_rank, price, size
                ) VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (opportunity_id, token_id, side, level_rank) DO NOTHING
                """,
                level_rows,
                page_size=200,
            )
        if tick_rows:
            psycopg2.extras.execute_batch(
                cur,
                """
                INSERT INTO opportunity_book_ticks (
                    opportunity_id, token_id, side, tick_seq,
                    event_type, price, size, ws_ts
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (opportunity_id, token_id, side, tick_seq) DO NOTHING
                """,
                tick_rows,
                page_size=200,
            )

    def save_order(
        self,
        mode: str,
        plan: TradePlan,
        leg_index: int,
        result: OrderResult,
        opportunity_id: Optional[int] = None,
    ) -> int:
        leg = plan.legs[leg_index]
        with self._lock, self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO orders (
                        opportunity_id, ts, mode, leg_index, event_id, market_id,
                        token_id, side, price, size, order_id, status, filled_size, error
                    ) VALUES (%s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        opportunity_id, time.time(), mode, leg_index,
                        leg.event_id, leg.market_id, leg.token_id, leg.side,
                        leg.price, leg.size, result.order_id, result.status,
                        result.filled_size, result.error or "",
                    ),
                )
                row_id = int(cur.fetchone()[0])
            conn.commit()
        return row_id

    def recent_opportunities(self, limit: int = 20) -> list:
        with self._lock, self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM opportunities ORDER BY detected_at DESC LIMIT %s",
                    (limit,),
                )
                return cur.fetchall()

    def daily_opportunity_stats(self, limit: int = 30) -> list:
        with self._lock, self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM daily_opportunity_stats
                    ORDER BY stat_date DESC, strategy ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()
