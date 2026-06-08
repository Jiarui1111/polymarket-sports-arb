"""Gamma API 客户端：市场发现。

职责：
- 拉取 active=true & closed=false 的事件（含嵌套 markets）。
- 解析为内部 Event / Market 模型。
- 识别多 outcome / neg-risk / 同 event 下多 related markets。
- 支持标签过滤（如 Sports）、分页、限速与重试。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Iterable, List, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from models import Event, Market

logger = logging.getLogger("data.gamma")


class _RateLimiter:
    """简单令牌间隔限速：保证两次请求间隔 >= min_interval 秒。"""

    def __init__(self, min_interval: float = 0.2) -> None:
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


def _parse_json_list(raw: Any) -> List[str]:
    """Gamma 把 outcomes/clobTokenIds 等存成 JSON 字符串，这里安全解析。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _to_float(raw: Any, default: Optional[float] = None) -> Optional[float]:
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


class GammaClient:
    def __init__(self, base_url: str, min_liquidity: float = 0.0, request_interval: float = 0.2) -> None:
        self.base_url = base_url.rstrip("/")
        self.min_liquidity = min_liquidity
        self._limiter = _RateLimiter(request_interval)
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "poly-arb-mvp/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    )
    def _get(self, path: str, params: dict) -> Any:
        self._limiter.wait()
        resp = self._client.get(path, params=params)
        if resp.status_code == 429:
            logger.warning("Gamma 限速 429，退避重试中…")
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    def fetch_events(
        self,
        max_events: int = 500,
        tags: Optional[Iterable[str]] = None,
        page_size: int = 100,
    ) -> List[Event]:
        """分页拉取活跃事件并解析。tags 为空则不按标签过滤。"""
        tag_set = {t.lower() for t in tags} if tags else None
        events: List[Event] = []
        offset = 0
        while len(events) < max_events:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": page_size,
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            try:
                data = self._get("/events", params)
            except Exception as exc:  # 单页失败不致命，记录后停止分页
                logger.error("拉取事件失败 offset=%s: %s", offset, exc)
                break

            if not isinstance(data, list) or not data:
                break

            for raw_event in data:
                event = self._parse_event(raw_event, tag_set)
                if event is not None:
                    events.append(event)

            if len(data) < page_size:
                break  # 已到最后一页
            offset += page_size

        logger.info("Gamma 共发现可用事件 %d 个（含可交易市场）", len(events))
        return events[:max_events]

    def _parse_event(self, raw: dict, tag_set: Optional[set]) -> Optional[Event]:
        tags = self._parse_tags(raw.get("tags"))
        if tag_set is not None:
            if not any(t.lower() in tag_set for t in tags):
                return None

        markets: List[Market] = []
        for raw_market in raw.get("markets", []) or []:
            market = self._parse_market(raw_market)
            if market is not None:
                markets.append(market)

        if not markets:
            return None

        return Event(
            event_id=str(raw.get("id", "")),
            title=raw.get("title", "") or raw.get("slug", ""),
            slug=raw.get("slug", ""),
            neg_risk=bool(raw.get("negRisk") or raw.get("enableNegRisk")),
            tags=tags,
            markets=markets,
        )

    def _parse_market(self, raw: dict) -> Optional[Market]:
        # 只保留可下单、未关闭、开启订单簿的市场
        if raw.get("closed") or not raw.get("active", True):
            return None
        if not raw.get("enableOrderBook", True):
            return None

        token_ids = _parse_json_list(raw.get("clobTokenIds"))
        outcomes = _parse_json_list(raw.get("outcomes"))
        if not token_ids or not outcomes:
            return None

        liquidity = _to_float(raw.get("liquidityNum"), _to_float(raw.get("liquidity"), 0.0)) or 0.0

        prices = [p for p in (_to_float(x) for x in _parse_json_list(raw.get("outcomePrices"))) if p is not None]

        return Market(
            market_id=str(raw.get("id", "")),
            question=raw.get("question", ""),
            slug=raw.get("slug", ""),
            group_item_title=raw.get("groupItemTitle", "") or "",
            condition_id=raw.get("conditionId", "") or "",
            neg_risk_market_id=raw.get("negRiskMarketID", "") or "",
            outcomes=outcomes,
            clob_token_ids=token_ids,
            outcome_prices=prices,
            best_bid=_to_float(raw.get("bestBid")),
            best_ask=_to_float(raw.get("bestAsk")),
            liquidity=liquidity,
            volume=_to_float(raw.get("volumeNum"), _to_float(raw.get("volume"), 0.0)) or 0.0,
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            neg_risk=bool(raw.get("negRisk", False)),
        )

    @staticmethod
    def _parse_tags(raw: Any) -> List[str]:
        if not raw:
            return []
        result: List[str] = []
        if isinstance(raw, list):
            for t in raw:
                if isinstance(t, dict):
                    label = t.get("label") or t.get("slug")
                    if label:
                        result.append(str(label))
                elif isinstance(t, str):
                    result.append(t)
        return result
