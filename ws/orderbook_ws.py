"""WebSocket 实时订单簿。

连接 Polymarket CLOB market 频道，维护本地 orderbook 缓存：
- 首次收到 ``book`` 全量快照覆盖本地；
- 收到 ``price_change`` 增量更新对应价位；
- 断线自动重连并重新订阅；
- 缓存线程安全，供策略层随时读取快照。

同时提供一个 user 频道（成交确认）的轻量监听器，供执行器使用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import websockets

from models import OrderBook, PriceLevel

logger = logging.getLogger("ws.orderbook")


class OrderBookCache:
    """线程安全的订单簿缓存。"""

    def __init__(self) -> None:
        self._books: Dict[str, OrderBook] = {}
        self._lock = threading.Lock()

    def update_snapshot(self, book: OrderBook) -> None:
        with self._lock:
            self._books[book.token_id] = book

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

    def get(self, token_id: str) -> Optional[OrderBook]:
        with self._lock:
            return self._books.get(token_id)

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
        """更新订阅的 token 列表（去重）。下次（重）连接时生效。"""
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
        """主循环：保持连接，断线指数退避重连。"""
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
            # 若订阅列表变更，重启连接以重新订阅
            if self._resubscribe.is_set():
                logger.info("订阅列表变更，重建 WS 连接")
                await ws.close()
                return
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            except asyncio.TimeoutError:
                continue  # 靠 ping 维持，无消息也正常
            self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        # 消息可能是单条 dict 或 list
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
                # tick_size_change / last_trade_price 暂不影响套利簿，忽略
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
    """在后台线程里跑一个独立事件循环运行 WS。返回控制句柄。"""
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
