"""日志装配：统一格式 + 密钥脱敏过滤器。"""

from __future__ import annotations

import logging
import sys
from typing import IO

from app.security.redaction import RedactingFilter, SecretRedactor


def build_redacting_handler(redactor: SecretRedactor, stream: IO[str] | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter(redactor))
    return handler


def setup_logging(level: str, redactor: SecretRedactor) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(build_redacting_handler(redactor))
    root.setLevel(level.upper())
