"""集中式配置：全部从环境变量 / .env 读取。

安全准则：
- 私钥、API secret 等敏感信息只从环境变量读取，绝不硬编码。
- 提供 ``masked()`` 用于安全打印，永远不泄露敏感值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote_plus

from dotenv import load_dotenv

try:
    load_dotenv(encoding="utf-8")
except UnicodeDecodeError:
    try:
        load_dotenv(encoding="cp936")
    except UnicodeDecodeError:
        load_dotenv(encoding="latin-1")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _get_float(key: str, default: float) -> float:
    raw = _get(key)
    try:
        return float(raw) if raw != "" else default
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    raw = _get(key)
    try:
        return int(raw) if raw != "" else default
    except ValueError:
        return default


def _get_list(key: str) -> List[str]:
    raw = _get(key)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _get_list_default(key: str, default: List[str]) -> List[str]:
    values = _get_list(key)
    return values if values else list(default)


def _get_bool(key: str, default: bool = False) -> bool:
    raw = _get(key).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class Config:
    # 运行模式
    trade_mode: str = field(default_factory=lambda: _get("TRADE_MODE", "dry_run").lower())

    # 凭证（敏感）
    private_key: str = field(default_factory=lambda: _get("POLY_PRIVATE_KEY"))
    funder_address: str = field(default_factory=lambda: _get("POLY_FUNDER_ADDRESS"))
    signature_type: int = field(default_factory=lambda: _get_int("POLY_SIGNATURE_TYPE", 0))
    api_key: str = field(default_factory=lambda: _get("POLY_API_KEY"))
    api_secret: str = field(default_factory=lambda: _get("POLY_API_SECRET"))
    api_passphrase: str = field(default_factory=lambda: _get("POLY_API_PASSPHRASE"))

    # 端点
    gamma_base_url: str = field(default_factory=lambda: _get("GAMMA_BASE_URL", "https://gamma-api.polymarket.com"))
    clob_base_url: str = field(default_factory=lambda: _get("CLOB_BASE_URL", "https://clob.polymarket.com"))
    clob_ws_url: str = field(default_factory=lambda: _get("CLOB_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws"))
    chain_id: int = field(default_factory=lambda: _get_int("CHAIN_ID", 137))

    # 市场发现
    market_tags: List[str] = field(default_factory=lambda: _get_list("MARKET_TAGS"))
    max_events: int = field(default_factory=lambda: _get_int("MAX_EVENTS", 500))
    min_market_liquidity: float = field(default_factory=lambda: _get_float("MIN_MARKET_LIQUIDITY", 500.0))
    target_market_filter_enabled: bool = field(default_factory=lambda: _get_bool("TARGET_MARKET_FILTER_ENABLED", True))
    target_min_outcomes: int = field(default_factory=lambda: _get_int("TARGET_MIN_OUTCOMES", 3))
    target_market_keywords: List[str] = field(default_factory=lambda: _get_list_default("TARGET_MARKET_KEYWORDS", [
        "champion",
        "winner",
        "group winner",
        "market cap",
        "deliveries",
        "inflation",
        "bracket",
        "nominee",
        "election",
        "ranking",
        "top scorer",
        "best",
        "most",
        "which",
        "how many",
        "ipo",
        "crypto",
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "xrp",
        "price",
        "range",
        "above",
        "below",
        "presidential",
        "nba",
        "mlb",
        "world cup",
        "tesla",
        "ai model",
    ]))

    # 策略阈值
    min_edge: float = field(default_factory=lambda: _get_float("MIN_EDGE", 0.01))
    slippage_buffer: float = field(default_factory=lambda: _get_float("SLIPPAGE_BUFFER", 0.005))
    enable_complement_strategy: bool = field(default_factory=lambda: _get_bool("ENABLE_COMPLEMENT_STRATEGY", False))
    enable_yes_complete_set: bool = field(default_factory=lambda: _get_bool("ENABLE_YES_COMPLETE_SET", True))
    enable_equal_no_basket: bool = field(default_factory=lambda: _get_bool("ENABLE_EQUAL_NO_BASKET", True))
    enable_unequal_no_basket: bool = field(default_factory=lambda: _get_bool("ENABLE_UNEQUAL_NO_BASKET", True))

    # 风控
    risk_max_order_usd: float = field(default_factory=lambda: _get_float("RISK_MAX_ORDER_USD", 50.0))
    risk_max_event_exposure_usd: float = field(default_factory=lambda: _get_float("RISK_MAX_EVENT_EXPOSURE_USD", 200.0))
    risk_max_total_exposure_usd: float = field(default_factory=lambda: _get_float("RISK_MAX_TOTAL_EXPOSURE_USD", 1000.0))
    risk_min_edge: float = field(default_factory=lambda: _get_float("RISK_MIN_EDGE", 0.01))
    risk_min_liquidity: float = field(default_factory=lambda: _get_float("RISK_MIN_LIQUIDITY", 500.0))
    risk_max_slippage: float = field(default_factory=lambda: _get_float("RISK_MAX_SLIPPAGE", 0.02))
    order_timeout_sec: float = field(default_factory=lambda: _get_float("ORDER_TIMEOUT_SEC", 20.0))

    # 成本
    fee_rate: float = field(default_factory=lambda: _get_float("FEE_RATE", 0.0))

    # 运行
    scan_interval_sec: float = field(default_factory=lambda: _get_float("SCAN_INTERVAL_SEC", 15.0))
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO").upper())

    # 数据库：本地 false 只打日志；服务器 true 写 PostgreSQL
    db_enabled: bool = field(default_factory=lambda: _get_bool("DB_ENABLED", False))

    # PostgreSQL（DATABASE_URL 优先，仅 DB_ENABLED=true 时需要）
    database_url: str = field(default_factory=lambda: _get("DATABASE_URL"))
    pg_host: str = field(default_factory=lambda: _get("PG_HOST", "localhost"))
    pg_port: int = field(default_factory=lambda: _get_int("PG_PORT", 5432))
    pg_user: str = field(default_factory=lambda: _get("PG_USER", "postgres"))
    pg_password: str = field(default_factory=lambda: _get("PG_PASSWORD"))
    pg_database: str = field(default_factory=lambda: _get("PG_DATABASE", "polymarket_arb"))
    pg_sslmode: str = field(default_factory=lambda: _get("PG_SSLMODE", "prefer"))

    # 机会落库：订单簿深度与 WS tick 条数
    book_level_depth: int = field(default_factory=lambda: _get_int("BOOK_LEVEL_DEPTH", 5))
    book_tick_depth: int = field(default_factory=lambda: _get_int("BOOK_TICK_DEPTH", 5))

    @property
    def pg_dsn(self) -> str:
        if self.database_url:
            return self.database_url
        if self.pg_host and self.pg_user and self.pg_database:
            user = quote_plus(self.pg_user)
            if self.pg_password:
                auth = f"{user}:{quote_plus(self.pg_password)}"
            else:
                auth = user
            return (
                f"postgresql://{auth}@{self.pg_host}:{self.pg_port}/{self.pg_database}"
                f"?sslmode={self.pg_sslmode}"
            )
        return ""

    @property
    def is_real(self) -> bool:
        return self.trade_mode == "real"

    @property
    def has_credentials(self) -> bool:
        return bool(self.private_key)

    def validate_for_real(self) -> List[str]:
        """返回 real 模式下缺失/不合法的配置项列表（不含敏感值）。"""
        problems: List[str] = []
        if not self.private_key:
            problems.append("POLY_PRIVATE_KEY 未设置（real 模式必需）")
        if self.private_key and not self.private_key.startswith("0x"):
            problems.append("POLY_PRIVATE_KEY 必须以 0x 开头")
        if self.risk_max_order_usd <= 0:
            problems.append("RISK_MAX_ORDER_USD 必须 > 0")
        if self.signature_type not in (0, 1, 2):
            problems.append("POLY_SIGNATURE_TYPE 必须是 0/1/2")
        return problems

    def masked(self) -> dict:
        """用于安全日志打印的配置快照，敏感字段全部脱敏。"""
        def mask(v: str) -> str:
            return "***set***" if v else "(empty)"

        return {
            "trade_mode": self.trade_mode,
            "private_key": mask(self.private_key),
            "funder_address": self.funder_address or "(derive)",
            "signature_type": self.signature_type,
            "api_key": mask(self.api_key),
            "api_secret": mask(self.api_secret),
            "api_passphrase": mask(self.api_passphrase),
            "gamma_base_url": self.gamma_base_url,
            "clob_base_url": self.clob_base_url,
            "clob_ws_url": self.clob_ws_url,
            "chain_id": self.chain_id,
            "market_tags": self.market_tags,
            "max_events": self.max_events,
            "min_market_liquidity": self.min_market_liquidity,
            "target_market_filter_enabled": self.target_market_filter_enabled,
            "target_min_outcomes": self.target_min_outcomes,
            "target_market_keywords": self.target_market_keywords,
            "min_edge": self.min_edge,
            "slippage_buffer": self.slippage_buffer,
            "strategies": {
                "complement": self.enable_complement_strategy,
                "yes_complete_set": self.enable_yes_complete_set,
                "equal_no_basket": self.enable_equal_no_basket,
                "unequal_no_basket": self.enable_unequal_no_basket,
            },
            "risk": {
                "max_order_usd": self.risk_max_order_usd,
                "max_event_exposure_usd": self.risk_max_event_exposure_usd,
                "max_total_exposure_usd": self.risk_max_total_exposure_usd,
                "min_edge": self.risk_min_edge,
                "min_liquidity": self.risk_min_liquidity,
                "max_slippage": self.risk_max_slippage,
                "order_timeout_sec": self.order_timeout_sec,
            },
            "fee_rate": self.fee_rate,
            "scan_interval_sec": self.scan_interval_sec,
            "log_level": self.log_level,
            "db_enabled": self.db_enabled,
            "pg_host": self.pg_host,
            "pg_port": self.pg_port,
            "pg_user": self.pg_user,
            "pg_password": mask(self.pg_password),
            "pg_database": self.pg_database,
            "database_url": mask(self.database_url),
            "book_level_depth": self.book_level_depth,
            "book_tick_depth": self.book_tick_depth,
        }


_config: Optional[Config] = None


def get_config() -> Config:
    """单例式获取配置。"""
    global _config
    if _config is None:
        _config = Config()
    return _config
