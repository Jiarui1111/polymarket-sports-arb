"""共享数据模型（dataclass）。

这些结构在各模块间传递：市场发现 -> 套利检测 -> 风控 -> 执行。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Market:
    """单个 Polymarket 市场（通常对应一个二元 YES/NO 问题）。

    在多 outcome 事件里，一个 event 包含多个这样的市场，
    每个市场代表一个互斥结果（例如某支球队夺冠）。
    """

    market_id: str                      # gamma market id
    question: str                       # 市场问题文本
    slug: str = ""
    group_item_title: str = ""          # 在 event 内的短标题（如队名）
    condition_id: str = ""              # CLOB condition id
    neg_risk_market_id: str = ""
    outcomes: List[str] = field(default_factory=list)        # ["Yes", "No"]
    clob_token_ids: List[str] = field(default_factory=list)  # 与 outcomes 对应的 token id
    outcome_prices: List[float] = field(default_factory=list)
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    liquidity: float = 0.0
    volume: float = 0.0
    active: bool = True
    closed: bool = False
    neg_risk: bool = False

    @property
    def yes_token_id(self) -> Optional[str]:
        """返回 YES 一侧的 token id（约定 outcomes[0] 为 Yes）。"""
        if not self.clob_token_ids:
            return None
        for i, o in enumerate(self.outcomes):
            if o.strip().lower() in ("yes", "true"):
                return self.clob_token_ids[i] if i < len(self.clob_token_ids) else None
        return self.clob_token_ids[0]

    @property
    def no_token_id(self) -> Optional[str]:
        if not self.clob_token_ids:
            return None
        for i, o in enumerate(self.outcomes):
            if o.strip().lower() in ("no", "false"):
                return self.clob_token_ids[i] if i < len(self.clob_token_ids) else None
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None


@dataclass
class Event:
    """一个 Polymarket 事件，可能聚合多个 related markets。"""

    event_id: str
    title: str
    slug: str = ""
    neg_risk: bool = False              # 是否为互斥多结果(neg risk)事件
    tags: List[str] = field(default_factory=list)
    markets: List[Market] = field(default_factory=list)

    @property
    def is_multi_outcome(self) -> bool:
        return len(self.markets) >= 2


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """单个 token 的订单簿快照。bids 降序、asks 升序。"""

    token_id: str
    bids: List[PriceLevel] = field(default_factory=list)
    asks: List[PriceLevel] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[PriceLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[PriceLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2.0
        return None

    def cost_to_buy(self, target_size: float) -> Optional[tuple[float, float]]:
        """吃 ask 买入 target_size 张的总成本和加权均价。

        返回 (avg_price, filled_size)。若深度不足，filled_size < target_size。
        """
        remaining = target_size
        cost = 0.0
        filled = 0.0
        for level in self.asks:
            take = min(remaining, level.size)
            cost += take * level.price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if filled <= 0:
            return None
        return cost / filled, filled

    def proceeds_to_sell(self, target_size: float) -> Optional[tuple[float, float]]:
        """吃 bid 卖出 target_size 张的总收入和加权均价。"""
        remaining = target_size
        proceeds = 0.0
        filled = 0.0
        for level in self.bids:
            take = min(remaining, level.size)
            proceeds += take * level.price
            filled += take
            remaining -= take
            if remaining <= 1e-9:
                break
        if filled <= 0:
            return None
        return proceeds / filled, filled


@dataclass
class TradeLeg:
    """套利计划中的一条腿（一次下单）。"""

    event_id: str
    market_id: str
    market_question: str
    token_id: str
    outcome: str                # 该 token 对应的 outcome 名（Yes/No/队名）
    side: str                   # BUY / SELL
    price: float                # 计划限价
    size: float                 # 张数
    available_depth: float      # 该价位可成交深度（张）

    @property
    def notional(self) -> float:
        """名义成本/金额 (USDC)。BUY=price*size；SELL 同样按 price*size 计。"""
        return self.price * self.size


@dataclass
class TradePlan:
    """一次结构套利机会的完整交易计划（多腿）。"""

    strategy: str               # 策略类型标识
    event_id: str
    event_title: str
    legs: List[TradeLeg] = field(default_factory=list)

    est_cost: float = 0.0       # 预计总成本 (USDC)
    est_max_payout: float = 0.0 # 预计最大回报 (USDC)
    est_profit: float = 0.0     # 预计利润 (USDC)
    edge: float = 0.0           # 单位 edge（概率口径，0-1）
    slippage: float = 0.0       # 估计滑点（概率口径）
    fee_cost: float = 0.0       # 估计手续费 (USDC)
    min_depth: float = 0.0      # 各腿可成交深度的最小值（张）
    notes: str = ""
    ts: float = field(default_factory=time.time)

    def summary_lines(self) -> List[str]:
        lines = [
            f"[机会] 策略={self.strategy} | 事件={self.event_title}",
            f"  预计成本={self.est_cost:.4f} USDC | 最大回报={self.est_max_payout:.4f} | "
            f"利润={self.est_profit:.4f} | edge={self.edge*100:.2f}% | 滑点={self.slippage*100:.2f}% | "
            f"手续费={self.fee_cost:.4f} | 最小深度={self.min_depth:.1f}张",
        ]
        for leg in self.legs:
            lines.append(
                f"    - {leg.side:4s} {leg.size:.1f} @ {leg.price:.4f} | {leg.outcome} "
                f"| token={leg.token_id[:10]}… | 深度={leg.available_depth:.1f} | {leg.market_question[:48]}"
            )
        if self.notes:
            lines.append(f"  备注: {self.notes}")
        return lines


@dataclass
class OrderResult:
    """下单结果。"""

    success: bool
    order_id: Optional[str] = None
    status: str = ""            # live / matched / filled / canceled / failed / simulated
    filled_size: float = 0.0
    error: str = ""
    raw: Optional[dict] = None
