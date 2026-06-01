"""数据库入口：默认使用 PostgreSQL。"""
from storage.pg_database import Database

__all__ = ["Database"]
