"""数据库入口：按 DB_ENABLED 选择 PostgreSQL 或空实现。"""
from __future__ import annotations

from config import Config
from storage.null_database import NullDatabase
from storage.pg_database import Database as PgDatabase

__all__ = ["Database", "create_database"]

# 类型别名：执行器只依赖 save_* 接口
Database = PgDatabase


def create_database(cfg: Config):
    if cfg.db_enabled:
        return PgDatabase(cfg)
    return NullDatabase()
