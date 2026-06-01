"""CLOB REST 客户端封装。

基于官方 py-clob-client SDK，提供：
- 只读行情：订单簿、价格、中间价（无需凭证，dry-run 可用）。
- 交易：限价单(GTC)、市价/即时单(FOK/FAK)、撤单（需凭证）。

设计原则：
- 读与写分离：dry-run 模式下完全不需要私钥。
- 凭证只来自 config（来自 .env），不在此硬编码。
- 所有网络调用带重试与限速。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from config import Config
from models import OrderBook, OrderResult, PriceLevel

logger = logging.getLogger("data.clob")

# 延迟导入 SDK，避免无凭证场景下的副作用
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BookParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL


class _RateLimiter:
    def __init__(self, min_interval: float = 0.1) -> None:
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


class ClobMarketClient:
    """封装行情读取 + 下单/撤单。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._limiter = _RateLimiter(0.1)
        self._trade_ready = False
        self._tick_cache: dict[str, float] = {}

        # 只读 client：无私钥也可用于行情
        self.client = ClobClient(host=cfg.clob_base_url, chain_id=cfg.chain_id)

        if cfg.is_real and cfg.has_credentials:
            self._init_trading_client()
        else:
            logger.info("CLOB 客户端以只读模式初始化（dry-run 或无凭证）")

    # ---------------- 交易客户端初始化 ----------------
    def _init_trading_client(self) -> None:
        """用私钥初始化可签名下单的 client，并装载 L2 API 凭证。"""
        try:
            kwargs = dict(
                host=self.cfg.clob_base_url,
                key=self.cfg.private_key,
                chain_id=self.cfg.chain_id,
                signature_type=self.cfg.signature_type,
            )
            if self.cfg.funder_address:
                kwargs["funder"] = self.cfg.funder_address
            self.client = ClobClient(**kwargs)

            # 装载 / 派生 L2 API 凭证
            if self.cfg.api_key and self.cfg.api_secret and self.cfg.api_passphrase:
                creds = ApiCreds(
                    api_key=self.cfg.api_key,
                    api_secret=self.cfg.api_secret,
                    api_passphrase=self.cfg.api_passphrase,
                )
            else:
                logger.info("未提供 API 凭证，正在从私钥派生 …")
                creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            self._trade_ready = True
            logger.info("CLOB 交易客户端就绪（地址 %s）", _short(self.client.get_address()))
        except Exception as exc:
            logger.error("初始化交易客户端失败，将退回只读模式: %s", exc)
            self._trade_ready = False

    @property
    def trade_ready(self) -> bool:
        return self._trade_ready

    # ---------------- 行情读取 ----------------
    @retry(reraise=True, stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
           retry=retry_if_exception_type(Exception))
    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        self._limiter.wait()
        raw = self.client.get_order_book(token_id)
        return _parse_book(token_id, raw)

    def get_order_books(self, token_ids: List[str]) -> dict[str, OrderBook]:
        """批量获取订单簿，失败的 token 跳过。"""
        result: dict[str, OrderBook] = {}
        if not token_ids:
            return result
        try:
            self._limiter.wait()
            params = [BookParams(token_id=t) for t in token_ids]
            books = self.client.get_order_books(params=params)
            for b in books:
                tid = getattr(b, "asset_id", None) or getattr(b, "market", None)
                if tid:
                    result[str(tid)] = _parse_book(str(tid), b)
        except Exception as exc:
            logger.warning("批量获取订单簿失败，回退到逐个获取: %s", exc)
            for t in token_ids:
                try:
                    book = self.get_order_book(t)
                    if book:
                        result[t] = book
                except Exception as e:
                    logger.debug("token %s 订单簿获取失败: %s", _short(t), e)
        return result

    def get_tick_size(self, token_id: str) -> float:
        if token_id in self._tick_cache:
            return self._tick_cache[token_id]
        try:
            self._limiter.wait()
            tick = float(self.client.get_tick_size(token_id))
        except Exception:
            tick = 0.01
        self._tick_cache[token_id] = tick
        return tick

    # ---------------- 下单 ----------------
    def place_limit_order(
        self, token_id: str, side: str, price: float, size: float, order_type: str = "GTC"
    ) -> OrderResult:
        """下限价单。order_type: GTC / GTD / FOK / FAK。"""
        if not self._trade_ready:
            return OrderResult(success=False, status="failed", error="交易客户端未就绪（缺凭证或非 real 模式）")

        side_const = BUY if side.upper() == "BUY" else SELL
        ot = _order_type(order_type)
        try:
            self._limiter.wait()
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                size=round(size, 2),
                side=side_const,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, ot)
            return _parse_order_resp(resp)
        except Exception as exc:
            logger.error("下单失败 token=%s side=%s: %s", _short(token_id), side, exc)
            return OrderResult(success=False, status="failed", error=str(exc))

    def place_market_order(
        self, token_id: str, side: str, amount: float, order_type: str = "FOK", price: Optional[float] = None
    ) -> OrderResult:
        """下市价/即时单。BUY 时 amount 为 USDC 金额，SELL 时为张数。"""
        if not self._trade_ready:
            return OrderResult(success=False, status="failed", error="交易客户端未就绪（缺凭证或非 real 模式）")

        side_const = BUY if side.upper() == "BUY" else SELL
        ot = _order_type(order_type if order_type in ("FOK", "FAK") else "FOK")
        try:
            self._limiter.wait()
            kwargs = dict(token_id=token_id, amount=round(amount, 2), side=side_const, order_type=ot)
            if price is not None:
                kwargs["price"] = round(price, 4)
            market_args = MarketOrderArgs(**kwargs)
            signed = self.client.create_market_order(market_args)
            resp = self.client.post_order(signed, ot)
            return _parse_order_resp(resp)
        except Exception as exc:
            logger.error("市价单失败 token=%s side=%s: %s", _short(token_id), side, exc)
            return OrderResult(success=False, status="failed", error=str(exc))

    # ---------------- 撤单 / 查询 ----------------
    def cancel_order(self, order_id: str) -> bool:
        if not self._trade_ready:
            return False
        try:
            self._limiter.wait()
            self.client.cancel(order_id)
            logger.info("已撤单 %s", order_id)
            return True
        except Exception as exc:
            logger.error("撤单失败 %s: %s", order_id, exc)
            return False

    def cancel_orders(self, order_ids: List[str]) -> bool:
        if not self._trade_ready or not order_ids:
            return False
        try:
            self._limiter.wait()
            self.client.cancel_orders(order_ids)
            return True
        except Exception as exc:
            logger.error("批量撤单失败: %s", exc)
            return False

    def get_order(self, order_id: str) -> Optional[dict]:
        if not self._trade_ready:
            return None
        try:
            self._limiter.wait()
            return self.client.get_order(order_id)
        except Exception as exc:
            logger.debug("查询订单失败 %s: %s", order_id, exc)
            return None


# ---------------- 解析辅助 ----------------
def _parse_book(token_id: str, raw) -> OrderBook:
    """把 SDK 的 OrderBookSummary 解析为内部 OrderBook（bids 降序、asks 升序）。"""
    def levels(items) -> List[PriceLevel]:
        out: List[PriceLevel] = []
        for it in items or []:
            price = float(getattr(it, "price", None) if not isinstance(it, dict) else it.get("price"))
            size = float(getattr(it, "size", None) if not isinstance(it, dict) else it.get("size"))
            out.append(PriceLevel(price=price, size=size))
        return out

    bids = levels(getattr(raw, "bids", None) if not isinstance(raw, dict) else raw.get("bids"))
    asks = levels(getattr(raw, "asks", None) if not isinstance(raw, dict) else raw.get("asks"))
    # API 返回的 bids 可能升序、asks 可能降序，统一排序
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBook(token_id=token_id, bids=bids, asks=asks, ts=time.time())


def _order_type(name: str) -> "OrderType":
    name = name.upper()
    return {
        "GTC": OrderType.GTC,
        "GTD": OrderType.GTD,
        "FOK": OrderType.FOK,
        "FAK": OrderType.FAK,
    }.get(name, OrderType.GTC)


def _parse_order_resp(resp) -> OrderResult:
    if not isinstance(resp, dict):
        resp = {"raw": str(resp)}
    success = bool(resp.get("success", True)) and not resp.get("errorMsg")
    order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id")
    status = resp.get("status") or ("live" if success else "failed")
    err = resp.get("errorMsg", "") or ("" if success else "unknown")
    filled = 0.0
    try:
        filled = float(resp.get("makingAmount", 0) or 0)
    except (ValueError, TypeError):
        filled = 0.0
    return OrderResult(
        success=success, order_id=str(order_id) if order_id else None,
        status=str(status), filled_size=filled, error=str(err), raw=resp,
    )


def _short(s: Optional[str]) -> str:
    if not s:
        return "?"
    return s[:10] + "…" if len(s) > 12 else s
