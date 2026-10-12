"""简单的每用户滑动窗口限流。"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Hashable


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: float = 60.0) -> None:
        if max_calls < 1:
            raise ValueError("max_calls 必须大于 0")
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[Hashable, deque[float]] = defaultdict(deque)

    def allow(self, key: Hashable) -> bool:
        """尝试为 key 记录一次调用；超过窗口内配额则返回 False。"""
        now = time.monotonic()
        bucket = self._calls[key]
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True
