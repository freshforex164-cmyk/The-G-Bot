"""Small in-memory sliding-window limiter for login endpoints (per user id).

One service instance holds the state; a restart clears it, which is acceptable for brute-force
protection on a single Render instance."""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable


def parse_rate(text: str, default: tuple[int, float]) -> tuple[int, float]:
    """'5/900' -> (5 attempts, 900 seconds). Bad values fall back to the default."""
    try:
        count, seconds = str(text).split("/", 1)
        n, w = int(count), float(seconds)
        if n >= 1 and w > 0:
            return n, w
    except (ValueError, TypeError):
        pass
    return default


class RateLimiter:
    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float:
        """Record one attempt. Returns 0 when allowed, otherwise seconds until the next attempt is allowed."""
        now = self._clock()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return max(1.0, math.ceil(self.window - (now - q[0])))
        q.append(now)
        if len(self._hits) > 10000:          # bound memory: forget idle keys
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                del self._hits[k]
        return 0.0


def wait_text(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"Too many sign-in attempts. Wait {minutes} minute{'s' if minutes != 1 else ''} and try again."
