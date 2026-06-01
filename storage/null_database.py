"""本地/无库模式：不落库，仅满足执行器接口。"""
from __future__ import annotations

import logging
from typing import Optional

from models import OrderResult, TradePlan
from storage.book_capture import OpportunityBookContext

logger = logging.getLogger("storage.null")


class NullDatabase:
    """DB_ENABLED=false 时使用，不连接 PostgreSQL。"""

    def __init__(self) -> None:
        logger.info("数据库已关闭（DB_ENABLED=false），机会与订单仅写日志")

    def close(self) -> None:
        pass

    def save_opportunity(
        self,
        plan: TradePlan,
        book_ctx: Optional[OpportunityBookContext] = None,
    ) -> int:
        return 0

    def save_order(
        self,
        mode: str,
        plan: TradePlan,
        leg_index: int,
        result: OrderResult,
        opportunity_id: Optional[int] = None,
    ) -> int:
        return 0

    def recent_opportunities(self, limit: int = 20) -> list:
        return []
