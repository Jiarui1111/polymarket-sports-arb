"""WebSocket 实时订单簿。

连接 Polymarket CLOB market 频道，维护本地 orderbook 缓存：
- 首次收到 ``book`` 全量快照覆盖本地；
- 收到 ``price_change`` 增量更新对应价位，并写入 tick 环形缓冲；
- 断线自动重连并重新订阅；
- 缓存线程安全，供策略层随时读取快照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

import websockets

from models import OrderBook, PriceLevel

logger = logging.getLogger("ws.orderbook")


@dataclass
class BookTick:
    """单条盘口变动（来自 WS price_change 或 book 快照的档位）。"""

    token_id: str
    side: str  # bid | ask
    price: float
    size: float
    event_type: str
    ts: float


class TickHistory:
    """每个 token 的 bid/ask 各保留最近 N 条 WS tick（环形队列）。"""

    def __init__(self, maxlen: int = 5) -> None:
        self.maxlen = maxlen
        self._queues: Dict[Tuple[str, str], Deque[BookTick]] = {}
        self._lock = threading.Lock()

    def record(self, tick: BookTick) -> None:
        key = (tick.token_id, tick.side)
        with self._lock:
            if key not in self._queues:
                self._queues[key] = deque(maxlen=self.maxlen)
            self._queues[key].append(tick)

    def record_changes(self, token_id: str, changes: List[dict], event_type: str = "price_change") -> None:
        now = time.time()
        for ch in changes:
            try:
                price = float(ch.get("price"))
                size = float(ch.get("size"))
                side_raw = str(ch.get("side", "")).upper()
            except (TypeError, ValueError):
                continue
            side = "bid" if side_raw in ("BUY", "BID") else "ask"
            self.record(BookTick(token_id, side, price, size, event_type, now))

    def record_book_top(self, token_id: str, book: OrderBook) -> None:
        """全量 book 时记录最优买卖档，便于无 price_change 时也有 tick。"""
        now = time.time()
        if book.best_bid:
            self.record(BookTick(token_id, "bid", book.best_bid.price, book.best_bid.size, "book", now))
        if book.best_ask:
            self.record(BookTick(token_id, "ask", book.best_ask.price, book.best_ask.size, "book", now))

    def get_ticks(self, token_id: str, tick_depth: int) -> Dict[str, List[BookTick]]:
        """返回 {bid: [...], ask: [...]} 各最多 tick_depth 条。"""
        out: Dict[str, List[BookTick]] = {"bid": [], "ask": []}
        with self._lock:
            for side in ("bid", "ask"):
                q = self._queues.get((token_id, side), deque())
                out[side] = list(q)[-tick_depth:]
        return out


class OrderBookCache:
    """线程安全的订单簿缓存。"""

    def __init__(self, tick_history: Optional[TickHistory] = None) -> None:
        self._books: Dict[str, OrderBook] = {}
        self._lock = threading.Lock()
        self.tick_history = tick_history or TickHistory()

    def update_snapshot(self, book: OrderBook) -> None:
        with self._lock:
            self._books[book.token_id] = book
        self.tick_history.record_book_top(book.token_id, book)

    def apply_price_change(self, token_id: str, changes: List[dict]) -> None:
        with self._lock:
            book = self._books.get(token_id)
            if book is None:
                book = OrderBook(token_id=token_id)
                self._books[token_id] = book
            for ch in changes:
                try:
                    price = float(ch.get("price"))
                    size = float(ch.get("size"))
                    side = str(ch.get("side", "")).upper()
                except (TypeError, ValueError):
                    continue
                levels = book.bids if side in ("BUY", "BID") else book.asks
                _upsert_level(levels, price, size)
            book.bids.sort(key=lambda x: x.price, reverse=True)
            book.asks.sort(key=lambda x: x.price)
            book.ts = time.time()
        self.tick_history.record_changes(token_id, changes)

    def get(self, token_id: str) -> Optional[OrderBook]:
        with self._lock:
            return self._books.get(token_id)

    def get_copy(self, token_id: str) -> Optional[OrderBook]:
        """深拷贝订单簿，供落库时避免并发修改。"""
        with self._lock:
            book = self._books.get(token_id)
            if not book:
                return None
            return OrderBook(
                token_id=book.token_id,
                bids=[PriceLevel(l.price, l.size) for l in book.bids],
                asks=[PriceLevel(l.price, l.size) for l in book.asks],
                ts=book.ts,
            )

    def snapshot(self) -> Dict[str, OrderBook]:
        with self._lock:
            return dict(self._books)

    def age(self, token_id: str) -> Optional[float]:
        with self._lock:
            book = self._books.get(token_id)
            return time.time() - book.ts if book else None


def _upsert_level(levels: List[PriceLevel], price: float, size: float) -> None:
    for i, lvl in enumerate(levels):
        if abs(lvl.price - price) < 1e-9:
            if size <= 0:
                levels.pop(i)
            else:
                lvl.size = size
            return
    if size > 0:
        levels.append(PriceLevel(price=price, size=size))


class OrderbookWebSocket:
    """market 频道客户端，自动重连 + 重订阅。"""

    def __init__(self, ws_base_url: str, cache: OrderBookCache) -> None:
        self.url = ws_base_url.rstrip("/") + "/market"
        self.cache = cache
        self._asset_ids: List[str] = []
        self._lock = threading.Lock()
        self._stop = asyncio.Event()
        self._ws = None
        self._resubscribe = asyncio.Event()

    def set_assets(self, asset_ids: List[str]) -> None:
        with self._lock:
            self._asset_ids = list(dict.fromkeys(asset_ids))
        self._resubscribe.set()

    def _current_assets(self) -> List[str]:
        with self._lock:
            return list(self._asset_ids)

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            assets = self._current_assets()
            if not assets:
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(
                    self.url, ping_interval=10, ping_timeout=10, close_timeout=5, max_size=8 * 1024 * 1024
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    await self._subscribe(ws, assets)
                    logger.info("订单簿 WS 已连接，订阅 %d 个 token", len(assets))
                    await self._consume(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    break
                logger.warning("订单簿 WS 断开: %s；%.1fs 后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None
        logger.info("订单簿 WS 已停止")

    async def _subscribe(self, ws, assets: List[str]) -> None:
        await ws.send(json.dumps({"assets_ids": assets, "type": "market"}))
        self._resubscribe.clear()

    async def _consume(self, ws) -> None:
        while not self._stop.is_set():
            if self._resubscribe.is_set():
                logger.info("订阅列表变更，重建 WS 连接")
                await ws.close()
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        messages = data if isinstance(data, list) else [data]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            event_type = msg.get("event_type") or msg.get("type")
            try:
                if event_type == "book":
                    self._on_book(msg)
                elif event_type == "price_change":
                    self._on_price_change(msg)
            except Exception as exc:
                logger.debug("处理 WS 消息异常: %s", exc)

    def _on_book(self, msg: dict) -> None:
        token_id = str(msg.get("asset_id") or msg.get("market") or "")
        if not token_id:
            return
        bids = [PriceLevel(float(b["price"]), float(b["size"])) for b in msg.get("bids", [])]
        asks = [PriceLevel(float(a["price"]), float(a["size"])) for a in msg.get("asks", [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        self.cache.update_snapshot(OrderBook(token_id=token_id, bids=bids, asks=asks, ts=time.time()))

    def _on_price_change(self, msg: dict) -> None:
        token_id = str(msg.get("asset_id") or msg.get("market") or "")
        changes = msg.get("changes") or msg.get("price_changes") or []
        if token_id and changes:
            self.cache.apply_price_change(token_id, changes)


def start_orderbook_ws_in_thread(ws_base_url: str, cache: OrderBookCache) -> "WsThreadHandle":
    handle = WsThreadHandle(ws_base_url, cache)
    handle.start()
    return handle


class WsThreadHandle:
    def __init__(self, ws_base_url: str, cache: OrderBookCache) -> None:
        self.ws = OrderbookWebSocket(ws_base_url, cache)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="orderbook-ws", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self.ws.run())
        except Exception as exc:
            logger.error("WS 线程异常退出: %s", exc)
        finally:
            self._loop.close()

    def set_assets(self, asset_ids: List[str]) -> None:
        self.ws.set_assets(asset_ids)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.ws.stop(), self._loop)
        if self._thread:
            self._thread.join(timeout=5)
