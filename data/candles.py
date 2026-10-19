"""Build and retain time-aligned OHLC candles from live Pocket Option ticks."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from config import (
    CANDLE_INTERVAL_SECONDS,
    MAX_CANDLES_PER_PAIR,
    POCKET_OPTION_TIME_OFFSET_SECONDS,
)


class CandleDataError(ValueError):
    """Raised when a broker tick or candle cannot be interpreted safely."""


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float


def _normalize_broker_timestamp(raw_timestamp: Any) -> int:
    try:
        timestamp = int(float(raw_timestamp))
    except (TypeError, ValueError, OverflowError):
        raise CandleDataError("Broker tick is missing a valid timestamp.") from None

    # Pocket Option may send milliseconds. BinaryOptionsToolsV2's own live
    # candle builder normalizes these timestamps and removes this platform offset.
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp - POCKET_OPTION_TIME_OFFSET_SECONDS


def _number(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CandleDataError(f"Broker candle is missing a valid {field}.") from None
    return result


class CandleBuilder:
    """Aggregate raw ticks into fixed-period OHLC candles and retain recent history."""

    def __init__(
        self,
        period_seconds: int = CANDLE_INTERVAL_SECONDS,
        max_candles: int = MAX_CANDLES_PER_PAIR,
    ) -> None:
        if period_seconds <= 0:
            raise ValueError("period_seconds must be greater than zero")
        if max_candles <= 0:
            raise ValueError("max_candles must be greater than zero")

        self.period_seconds = period_seconds
        self.max_candles = max_candles
        self.closed: deque[Candle] = deque(maxlen=max_candles)
        self.current: Candle | None = None
        self._latest_closed_time: int | None = None

    @property
    def forming_candle(self) -> Candle | None:
        """Return the current, still-open candle being updated by live ticks."""
        return self.current

    def seed(self, history: Iterable[Mapping[str, Any]]) -> int:
        """Seed from broker-compiled history, ignoring the current open time bucket."""
        current_bucket = int(time.time()) // self.period_seconds * self.period_seconds
        by_time: dict[int, Candle] = {}

        for row in history:
            if not isinstance(row, Mapping):
                continue
            raw_time = row.get("timestamp", row.get("time"))
            timestamp = _normalize_broker_timestamp(raw_time)
            bucket_time = timestamp // self.period_seconds * self.period_seconds
            if bucket_time >= current_bucket:
                continue
            by_time[bucket_time] = Candle(
                time=bucket_time,
                open=_number(row.get("open"), "open"),
                high=_number(row.get("high"), "high"),
                low=_number(row.get("low"), "low"),
                close=_number(row.get("close"), "close"),
            )

        for candle in sorted(by_time.values(), key=lambda item: item.time)[
            -self.max_candles :
        ]:
            self.closed.append(candle)
        if self.closed:
            self._latest_closed_time = self.closed[-1].time
        return len(self.closed)

    def update(self, tick: Mapping[str, Any]) -> Candle | None:
        """Add a raw tick; return a candle only when its time bucket has closed."""
        raw_time = tick.get("timestamp", tick.get("time"))
        timestamp = _normalize_broker_timestamp(raw_time)
        price = _number(tick.get("close", tick.get("price")), "price")
        bucket_time = timestamp // self.period_seconds * self.period_seconds

        if self.current is None:
            if (
                self._latest_closed_time is not None
                and bucket_time <= self._latest_closed_time
            ):
                return None
            self.current = Candle(
                time=bucket_time,
                open=price,
                high=price,
                low=price,
                close=price,
            )
            return None

        if bucket_time < self.current.time:
            return None

        if bucket_time == self.current.time:
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            return None

        closed_candle = self.current
        self.closed.append(closed_candle)
        self._latest_closed_time = closed_candle.time
        self.current = Candle(
            time=bucket_time,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        return closed_candle
