"""统一日志配置。

特性：
- 同时输出到控制台和 ``logs/app.log``（按大小滚动）。
- ``SecretRedactingFilter`` 自动屏蔽疑似私钥 / api secret，防止敏感信息落盘。
"""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

# 匹配常见敏感串：0x 开头的 64 位 hex（私钥）、长 base64 secret
_SECRET_PATTERNS = [
    re.compile(r"0x[a-fA-F0-9]{64}"),                 # 私钥
    re.compile(r"(?i)(secret|passphrase|api[_-]?key)\s*[=:]\s*\S+"),
]


class SecretRedactingFilter(logging.Filter):
    """在日志写出前对敏感串做脱敏，作为最后一道防线。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            redacted = msg
            for pat in _SECRET_PATTERNS:
                redacted = pat.sub("***REDACTED***", redacted)
            if redacted != msg:
                record.msg = redacted
                record.args = ()
        except Exception:
            pass
        return True


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    log_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(log_level)
    # 避免重复添加 handler
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = SecretRedactingFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(redactor)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # 降低第三方库噪音
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
