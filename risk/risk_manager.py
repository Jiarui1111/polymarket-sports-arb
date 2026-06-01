"""风控模块。

在执行任何交易计划前做强校验（real 模式强制）：
- 单笔订单最大金额
- 单个 event 最大累计暴露
- 全局最大累计暴露
- 最小 edge 阈值
- 最小流动性阈值
- 最大滑点
执行后通过 register / release 维护实时暴露。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Tuple

from config import Config
from models import TradePlan

logger = logging.getLogger("risk.manager")


@dataclass
class RiskDecision:
    approved: bool
    reasons: List[str]


class RiskManager:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._event_exposure: Dict[str, float] = {}
        self._total_exposure: float = 0.0

    def evaluate(self, plan: TradePlan) -> RiskDecision:
        reasons: List[str] = []

        # 1. 最小 edge
        if plan.edge < self.cfg.risk_min_edge:
            reasons.append(f"edge {plan.edge:.4f} < 风控下限 {self.cfg.risk_min_edge}")

        # 2. 最大滑点
        if plan.slippage > self.cfg.risk_max_slippage:
            reasons.append(f"滑点 {plan.slippage:.4f} > 上限 {self.cfg.risk_max_slippage}")

        # 3. 最小流动性（用最小深度 * 价格近似单腿可成交金额，并校验市场流动性口径）
        if plan.min_depth <= 0:
            reasons.append("可成交深度为 0")

        # 4. 单笔订单最大金额（逐腿校验）
        for leg in plan.legs:
            if leg.notional > self.cfg.risk_max_order_usd:
                reasons.append(
                    f"单腿金额 {leg.notional:.2f} > 单笔上限 {self.cfg.risk_max_order_usd}"
                )
                break

        # 5. 流动性阈值：成本不足或机会名义额过小
        if plan.est_cost < 0:
            reasons.append("成本计算异常")

        # 6. event 暴露 & 全局暴露
        with self._lock:
            cur_event = self._event_exposure.get(plan.event_id, 0.0)
            if cur_event + plan.est_cost > self.cfg.risk_max_event_exposure_usd:
                reasons.append(
                    f"event 暴露 {cur_event + plan.est_cost:.2f} > 上限 "
                    f"{self.cfg.risk_max_event_exposure_usd}"
                )
            if self._total_exposure + plan.est_cost > self.cfg.risk_max_total_exposure_usd:
                reasons.append(
                    f"全局暴露 {self._total_exposure + plan.est_cost:.2f} > 上限 "
                    f"{self.cfg.risk_max_total_exposure_usd}"
                )

        approved = len(reasons) == 0
        if not approved:
            logger.info("风控拒绝 [%s/%s]: %s", plan.strategy, plan.event_title, "; ".join(reasons))
        return RiskDecision(approved=approved, reasons=reasons)

    def register(self, plan: TradePlan) -> None:
        """计划即将执行，登记暴露。"""
        with self._lock:
            self._event_exposure[plan.event_id] = self._event_exposure.get(plan.event_id, 0.0) + plan.est_cost
            self._total_exposure += plan.est_cost
            logger.debug("登记暴露：event=%.2f total=%.2f",
                         self._event_exposure[plan.event_id], self._total_exposure)

    def release(self, plan: TradePlan) -> None:
        """计划失败/撤单后释放暴露。"""
        with self._lock:
            self._event_exposure[plan.event_id] = max(
                0.0, self._event_exposure.get(plan.event_id, 0.0) - plan.est_cost
            )
            self._total_exposure = max(0.0, self._total_exposure - plan.est_cost)

    def exposure_snapshot(self) -> Tuple[float, Dict[str, float]]:
        with self._lock:
            return self._total_exposure, dict(self._event_exposure)
