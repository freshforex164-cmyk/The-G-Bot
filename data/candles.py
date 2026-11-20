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

    # Pocket Option may send milliseconds. BinaryOptionsToolsV2 0.2.15 live
    # ticks carried a +2h platform offset; 0.2.16+ ticks and all compiled
    # history are true UTC. Remove the offset only when a timestamp is far
    # ahead of the clock, so both formats land in the correct minute bucket.
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    if timestamp - time.time() > POCKET_OPTION_TIME_OFFSET_SECONDS / 4:
        timestamp -= POCKET_OPTION_TIME_OFFSET_SECONDS
    return timestamp


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
        self._current_partial = False

    @property
    def forming_candle(self) -> Candle | None:
        """Return the current, still-open candle being updated by live ticks."""
        return self.current

    def seed(self, history: Iterable[Mapping[str, Any]]) -> int:
        """Seed closed history; the open bucket only supplies the forming candle.

        Safe to call after live ticks have started: live closed candles win,
        and history for the current bucket provides its true open/high/low so
        the first live candle is complete rather than partial.
        """
        current_bucket = int(time.time()) // self.period_seconds * self.period_seconds
        by_time: dict[int, Candle] = {}
        normalized_rows: list[tuple[int, Candle]] = []
        forming_rows: list[tuple[int, Candle]] = []

        for row in history:
            if not isinstance(row, Mapping):
                continue
            raw_time = row.get("timestamp", row.get("time"))
            timestamp = _normalize_broker_timestamp(raw_time)
            bucket_time = timestamp // self.period_seconds * self.period_seconds
            if bucket_time > current_bucket:
                continue
            candle = Candle(
                time=bucket_time,
                open=_number(row.get("open"), "open"),
                high=_number(row.get("high"), "high"),
                low=_number(row.get("low"), "low"),
                close=_number(row.get("close"), "close"),
            )
            if bucket_time == current_bucket:
                forming_rows.append((timestamp, candle))
            else:
                normalized_rows.append((timestamp, candle))

        def merge(rows: list[tuple[int, Candle]]) -> dict[int, Candle]:
            merged: dict[int, Candle] = {}
            for timestamp, candle in sorted(rows, key=lambda item: item[0]):
                bucket_time = timestamp // self.period_seconds * self.period_seconds
                existing = merged.get(bucket_time)
                if existing is None:
                    merged[bucket_time] = candle
                else:
                    existing.high = max(existing.high, candle.high)
                    existing.low = min(existing.low, candle.low)
                    existing.close = candle.close
            return merged

        by_time = merge(normalized_rows)
        for candle in self.closed:
            by_time[candle.time] = candle
        ordered = sorted(by_time.values(), key=lambda item: item.time)
        self.closed = deque(ordered[-self.max_candles :], maxlen=self.max_candles)
        if self.closed:
            self._latest_closed_time = self.closed[-1].time

        forming = merge(forming_rows).get(current_bucket)
        if forming is not None:
            if self.current is None:
                self.current = forming
                self._current_partial = False
            elif self.current.time == forming.time:
                self.current.open = forming.open
                self.current.high = max(self.current.high, forming.high)
                self.current.low = min(self.current.low, forming.low)
                self._current_partial = False
        return len(self.closed)

    def update(self, tick: Mapping[str, Any]) -> Candle | None:
        """Add a raw tick; return a candle only when its time bucket has closed.

        Candles start on whole periods. A bucket joined mid-way (first tick
        after a start, without history for that bucket) is never emitted.
        """
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
            self._current_partial = timestamp != bucket_time
            return None

        if bucket_time < self.current.time:
            return None

        if bucket_time == self.current.time:
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            return None

        closed_candle = self.current
        was_partial = self._current_partial
        self.current = Candle(
            time=bucket_time,
            open=price,
            high=price,
            low=price,
            close=price,
        )
        self._current_partial = False
        if was_partial:
            return None
        self.closed.append(closed_candle)
        self._latest_closed_time = closed_candle.time
        return closed_candle
