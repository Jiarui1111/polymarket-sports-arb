"""Polymarket 多 outcome 结构套利程序 - 启动入口。

用法示例：
    # 1) 复制并填写配置
    cp .env.example .env

    # 2) dry-run（默认，只模拟不下单）
    python main.py

    # 3) 单次扫描后退出
    python main.py --once

    # 4) 指定标签与最小 edge
    python main.py --tags Sports --min-edge 0.02

    # 5) 真实交易（务必先充分 dry-run；需要 --i-understand-real 显式确认）
    python main.py --mode real --i-understand-real

安全：私钥/凭证只从 .env 读取，任何日志都不会打印敏感值。
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Dict, List, Optional

from config import get_config
from data.clob_client import ClobMarketClient
from data.polymarket_gamma_client import GammaClient
from execution.order_executor import OrderExecutor
from logging_config import setup_logging
from models import Event, OrderBook
from risk.risk_manager import RiskManager
from storage.book_capture import capture_opportunity_books
from storage.db import Database
from strategy.multi_outcome_arbitrage import MultiOutcomeArbitrage
from ws.orderbook_ws import OrderBookCache, TickHistory, start_orderbook_ws_in_thread

logger = logging.getLogger("main")

_STALE_SEC = 10.0  # WS 快照超过该秒数视为陈旧，扫描前用 REST 补齐


class App:
    def __init__(self, cfg, args) -> None:
        self.cfg = cfg
        self.args = args
        self._stop = False

        self.db = Database(cfg)
        self.gamma = GammaClient(cfg.gamma_base_url, min_liquidity=cfg.min_market_liquidity)
        self.clob = ClobMarketClient(cfg)
        self.risk = RiskManager(cfg)
        self.strategy = MultiOutcomeArbitrage(cfg)
        self.executor = OrderExecutor(cfg, self.clob, self.risk, self.db)

        tick_history = TickHistory(maxlen=cfg.book_tick_depth)
        self.cache = OrderBookCache(tick_history=tick_history)
        self.ws = start_orderbook_ws_in_thread(cfg.clob_ws_url, self.cache)

        self.events: List[Event] = []
        self._seeded: Dict[str, OrderBook] = {}

    def request_stop(self, *_) -> None:
        logger.info("收到停止信号，正在优雅退出 …")
        self._stop = True

    # ---------------- 市场发现 ----------------
    def discover(self) -> None:
        tags = self.args.tags if self.args.tags else self.cfg.market_tags
        self.events = self.gamma.fetch_events(max_events=self.cfg.max_events, tags=tags or None)

        multi = sum(1 for e in self.events if e.neg_risk and len(e.markets) >= 2)
        logger.info("发现事件 %d 个，其中 neg-risk 多结果 %d 个", len(self.events), multi)

        token_ids = self._all_tokens()
        self.ws.set_assets(token_ids)
        logger.info("已向 WS 订阅 %d 个 token 的实时订单簿", len(token_ids))

    def _all_tokens(self) -> List[str]:
        tokens: List[str] = []
        for e in self.events:
            for m in e.markets:
                tokens.extend(m.clob_token_ids)
        return list(dict.fromkeys(tokens))

    # ---------------- 单轮扫描 ----------------
    def scan_once(self) -> int:
        self._seed_stale_books()

        def get_book(token_id: str) -> Optional[OrderBook]:
            book = self.cache.get(token_id)
            if book and (time.time() - book.ts) <= _STALE_SEC:
                return book
            return self._seeded.get(token_id) or book

        found = 0
        for event in self.events:
            try:
                plans = self.strategy.scan_event(event, get_book)
            except Exception as exc:
                logger.debug("扫描事件 %s 异常: %s", event.event_id, exc)
                continue
            for plan in plans:
                found += 1
                self._hydrate_cache_for_plan(plan)
                book_ctx = capture_opportunity_books(
                    self.cache,
                    self.cache.tick_history,
                    plan,
                    level_depth=self.cfg.book_level_depth,
                    tick_depth=self.cfg.book_tick_depth,
                )
                self.executor.handle_opportunity(plan, book_ctx)
        return found

    def _hydrate_cache_for_plan(self, plan) -> None:
        """机会触发前把 REST 补齐的订单簿灌入 cache，便于落库。"""
        for leg in plan.legs:
            tid = leg.token_id
            book = self.cache.get(tid)
            if book and (time.time() - book.ts) <= _STALE_SEC:
                continue
            seeded = self._seeded.get(tid)
            if seeded:
                self.cache.update_snapshot(seeded)

    def _seed_stale_books(self) -> None:
        """对 WS 尚未覆盖或陈旧的 token，用 REST 批量补齐快照。"""
        needed: List[str] = []
        for token_id in self._all_tokens():
            book = self.cache.get(token_id)
            if not book or (time.time() - book.ts) > _STALE_SEC:
                needed.append(token_id)
        if not needed:
            self._seeded = {}
            return
        # 控制单次 REST 数量，避免限速
        batch = needed[:300]
        logger.info("WS 未覆盖 %d 个 token，REST 补齐 %d 个", len(needed), len(batch))
        self._seeded = self.clob.get_order_books(batch)

    # ---------------- 主循环 ----------------
    def run(self) -> None:
        logger.info("配置快照（已脱敏）：%s", self.cfg.masked())
        self.discover()
        # 给 WS 一点时间收首批快照
        time.sleep(3.0)

        scan_count = 0
        while not self._stop:
            scan_count += 1
            t0 = time.time()
            try:
                found = self.scan_once()
                logger.info("第 %d 轮扫描完成，发现机会 %d 个，用时 %.1fs",
                            scan_count, found, time.time() - t0)
            except Exception as exc:
                logger.exception("扫描轮次异常：%s", exc)

            if self.args.once:
                break

            # 每 20 轮重新发现一次市场
            if scan_count % 20 == 0:
                try:
                    self.discover()
                except Exception as exc:
                    logger.error("重新发现市场失败：%s", exc)

            self._sleep(self.cfg.scan_interval_sec)

        self.shutdown()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop:
            time.sleep(0.2)

    def shutdown(self) -> None:
        logger.info("关闭中 …")
        try:
            self.ws.stop()
        except Exception:
            pass
        try:
            self.gamma.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        # 输出累计暴露
        total, per_event = self.risk.exposure_snapshot()
        logger.info("累计暴露 total=%.2f USDC，事件数=%d", total, len(per_event))
        logger.info("已退出。")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Polymarket 多 outcome 结构套利 MVP")
    p.add_argument("--mode", choices=["dry_run", "real"], help="运行模式，覆盖 .env 中 TRADE_MODE")
    p.add_argument("--once", action="store_true", help="只扫描一轮后退出")
    p.add_argument("--interval", type=float, help="扫描间隔秒，覆盖配置")
    p.add_argument("--max-events", type=int, help="最大拉取事件数，覆盖配置")
    p.add_argument("--tags", nargs="*", help="标签过滤，如 --tags Sports")
    p.add_argument("--min-edge", type=float, help="最小净 edge 阈值，覆盖配置")
    p.add_argument("--i-understand-real", action="store_true",
                   help="real 模式安全确认开关（缺失则强制回退 dry-run）")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = get_config()

    # CLI 覆盖配置
    if args.mode:
        cfg.trade_mode = args.mode
    if args.interval is not None:
        cfg.scan_interval_sec = args.interval
    if args.max_events is not None:
        cfg.max_events = args.max_events
    if args.min_edge is not None:
        cfg.min_edge = args.min_edge

    setup_logging(cfg.log_level)

    # real 模式安全闸门
    if cfg.is_real:
        if not args.i_understand_real:
            logger.error("real 模式必须显式加 --i-understand-real 确认；已强制回退 dry_run。")
            cfg.trade_mode = "dry_run"
        else:
            problems = cfg.validate_for_real()
            if problems:
                logger.error("real 配置校验失败，回退 dry_run：")
                for prob in problems:
                    logger.error("  - %s", prob)
                cfg.trade_mode = "dry_run"

    logger.info("==== 启动 Polymarket 结构套利 MVP | 模式=%s ====", cfg.trade_mode)

    app = App(cfg, args)
    signal.signal(signal.SIGINT, app.request_stop)
    try:
        signal.signal(signal.SIGTERM, app.request_stop)
    except (AttributeError, ValueError):
        pass

    try:
        app.run()
    except KeyboardInterrupt:
        app.request_stop()
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
