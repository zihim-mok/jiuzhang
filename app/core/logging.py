"""统一日志：密钥与凭证脱敏，绝不写入日志。"""
from __future__ import annotations

import logging
import re
import sys
from functools import lru_cache

REDACT_PATTERNS = [
    re.compile(r"(api[_-]?key\s*[=:]\s*)(\S+)", re.IGNORECASE),
    re.compile(r"(authorization\s*[=:]\s*Bearer\s+)(\S+)", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9]{16,})"),
]
_REDACT_REPL = r"\g<1>***REDACTED***"


class SensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in REDACT_PATTERNS:
            msg = pat.sub(_REDACT_REPL, msg)
        record.msg = msg
        record.args = None
        return True


@lru_cache(maxsize=1)
def get_logger(name: str = "app") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        handler.addFilter(SensitiveFilter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
