"""密钥脱敏：注册的密钥值与常见密钥形态在任何输出中被替换。"""

from __future__ import annotations

import logging
import re

# 常见 API Key 形态（OpenAI/DeepSeek/GLM 均为 sk- 前缀）
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


class SecretRedactor:
    """把已注册的密钥值从任意文本中抹除；同时按模式匹配未知密钥。"""

    def __init__(self) -> None:
        self._secrets: tuple[str, ...] = ()

    def register(self, secret: str) -> None:
        value = (secret or "").strip()
        if len(value) >= 8 and value not in self._secrets:
            self._secrets = self._secrets + (value,)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***REDACTED***")
        return _SECRET_PATTERN.sub("sk-***REDACTED***", text)


class RedactingFilter(logging.Filter):
    """挂在日志 Handler 上的最后一道防线：密钥不进入日志。"""

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redactor.redact(str(record.msg))
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(
                self._redactor.redact(arg) if isinstance(arg, str) else arg for arg in args
            )
        return True
