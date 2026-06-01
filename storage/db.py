"""SQLite 持久化：保存套利机会、下单计划、订单与成交。

使用标准库 sqlite3，线程安全用锁包裹（程序内并发量低，足够 MVP）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Optional

from models import OrderResult, TradePlan

logger = logging.getLogger("storage.db")


class Database:
    def __init__(self, path: str = "arb.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("SQLite 已就绪: %s", path)

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    strategy TEXT,
                    event_id TEXT,
                    event_title TEXT,
                    edge REAL,
                    est_cost REAL,
                    est_profit REAL,
                    est_max_payout REAL,
                    slippage REAL,
                    min_depth REAL,
                    plan_json TEXT
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    mode TEXT,
                    event_id TEXT,
                    market_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    price REAL,
                    size REAL,
                    order_id TEXT,
                    status TEXT,
                    filled_size REAL,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities(ts);
                CREATE INDEX IF NOT EXISTS idx_ord_ts ON orders(ts);
                """
            )

    def save_opportunity(self, plan: TradePlan) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO opportunities
                   (ts, strategy, event_id, event_title, edge, est_cost, est_profit,
                    est_max_payout, slippage, min_depth, plan_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.ts, plan.strategy, plan.event_id, plan.event_title, plan.edge,
                    plan.est_cost, plan.est_profit, plan.est_max_payout, plan.slippage,
                    plan.min_depth, json.dumps(_plan_to_dict(plan)),
                ),
            )
            return int(cur.lastrowid)

    def save_order(self, mode: str, plan: TradePlan, leg_index: int, result: OrderResult) -> int:
        leg = plan.legs[leg_index]
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO orders
                   (ts, mode, event_id, market_id, token_id, side, price, size,
                    order_id, status, filled_size, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(), mode, leg.event_id, leg.market_id, leg.token_id, leg.side,
                    leg.price, leg.size, result.order_id, result.status,
                    result.filled_size, result.error,
                ),
            )
            return int(cur.lastrowid)

    def recent_opportunities(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM opportunities ORDER BY ts DESC LIMIT ?", (limit,)
            )
            return cur.fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _plan_to_dict(plan: TradePlan) -> dict:
    return {
        "strategy": plan.strategy,
        "event_id": plan.event_id,
        "event_title": plan.event_title,
        "est_cost": plan.est_cost,
        "est_max_payout": plan.est_max_payout,
        "est_profit": plan.est_profit,
        "edge": plan.edge,
        "slippage": plan.slippage,
        "fee_cost": plan.fee_cost,
        "min_depth": plan.min_depth,
        "notes": plan.notes,
        "legs": [
            {
                "market_id": leg.market_id,
                "market_question": leg.market_question,
                "token_id": leg.token_id,
                "outcome": leg.outcome,
                "side": leg.side,
                "price": leg.price,
                "size": leg.size,
                "available_depth": leg.available_depth,
            }
            for leg in plan.legs
        ],
    }
