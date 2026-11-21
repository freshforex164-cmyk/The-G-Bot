"""Broker timestamp anchored to a monotonic clock; never adjust it backwards."""
from __future__ import annotations

import time
from math import isfinite


class BrokerClock:
    def __init__(self, monotonic=time.monotonic):
        self._monotonic = monotonic
        self._anchor: tuple[float, float] | None = None

    @property
    def synchronized(self):
        return self._anchor is not None

    def now(self):
        if self._anchor is None:
            return time.time()
        stamp, received = self._anchor
        return stamp + self._monotonic() - received

    def observe(self, stamp):
        stamp = float(stamp)
        if not isfinite(stamp) or stamp <= 0:
            raise ValueError("Broker timestamp must be positive and finite.")
        if self._anchor is None or stamp >= self.now():
            self._anchor = stamp, self._monotonic()