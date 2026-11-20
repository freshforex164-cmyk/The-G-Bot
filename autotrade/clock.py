"""Injectable clock so time-dependent rules (cooldown, daily limits, freshness) are testable."""
from __future__ import annotations

import time
from datetime import datetime, timezone


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)


def utc_day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
