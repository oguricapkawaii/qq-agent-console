"""文本安全处理。

所有 QQ 侧文本（消息、引用内容、文件名、网页内容等）均视为不可信输入：
- 入站：剥离 CQ 码与控制字符，防止伪造图片/语音/@ 与终端控制序列注入；
- 出站：中和模型输出中的 CQ 码前缀，防止 OneBot 实现把模型文本解析成富媒体；
- 分段：超过长度限制的回复自动拆分为多条消息。
"""

from __future__ import annotations

import re

_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]", re.S)
# 除换行(\n)与回车(\r)外的 C0/C1 控制字符
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

# 出站时用于打断 "[CQ:" 前缀的零宽空格
_ZWSP = "\u200b"


def sanitize_inbound_text(text: str) -> str:
    """清洗来自 QQ 的不可信文本：去 CQ 码、去控制字符、去首尾空白。"""
    cleaned = _CQ_CODE_RE.sub("", text or "")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    return cleaned.strip()


def sanitize_outbound_text(text: str) -> str:
    """清洗将要发往 QQ 的文本：防止被 OneBot 解析为 CQ 码。"""
    cleaned = (text or "").replace("[CQ:", f"[CQ{_ZWSP}:")
    return _CONTROL_CHARS_RE.sub("", cleaned)


def split_message(text: str, max_len: int) -> list[str]:
    """把长文本拆分为多条不超过 max_len 的消息段。

    优先在换行处切分，单行超长时硬切分。
    """
    if max_len <= 0:
        raise ValueError("max_len 必须为正数")
    cleaned = text.rstrip()
    if len(cleaned) <= max_len:
        return [cleaned]

    parts: list[str] = []
    current = ""
    for line in cleaned.split("\n"):
        while len(line) > max_len:  # 单行超长，硬切
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:max_len])
            line = line[max_len:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
