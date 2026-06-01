"""执行 db/sql 下全部迁移脚本（按文件名排序）。

用法:
    python -m db.migrate
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import psycopg2

from config import get_config

logger = logging.getLogger("db.migrate")
SQL_DIR = Path(__file__).resolve().parent / "sql"


def run_migrations() -> None:
    cfg = get_config()
    if not cfg.db_enabled:
        logger.error("DB_ENABLED=false，无需迁移。服务器请设置 DB_ENABLED=true")
        sys.exit(1)
    if not cfg.pg_dsn:
        logger.error("未配置 PostgreSQL（PG_* 或 DATABASE_URL）")
        sys.exit(1)

    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        logger.error("未找到 SQL 文件: %s", SQL_DIR)
        sys.exit(1)

    conn = psycopg2.connect(cfg.pg_dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for path in files:
                sql = path.read_text(encoding="utf-8")
                logger.info("执行 %s", path.name)
                cur.execute(sql)
        logger.info("迁移完成，共 %d 个文件", len(files))
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    run_migrations()
