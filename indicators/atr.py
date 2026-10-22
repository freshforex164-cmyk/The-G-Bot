"""Arithmetic mean true range aligned to closed candle indexes."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

import config
from data.candles import Candle


def calculate_atr(
    candles: Sequence[Candle], period: int = config.ATR_PERIOD
) -> list[float | None]:
    if period <= 0:
        raise ValueError("ATR period must be greater than zero")
    true_ranges: list[float] = []
    values: list[float | None] = [None] * len(candles)
    window: deque[float] = deque(maxlen=period)
    for index, candle in enumerate(candles):
        high, low = float(candle.high), float(candle.low)
        previous_close = float(candles[index - 1].close) if index else float(candle.close)
        tr = max(high - low, abs(high - previous_close), abs(low - previous_close))
        true_ranges.append(tr)
        window.append(tr)
        if len(window) == period:
            values[index] = sum(window) / period
    return values
