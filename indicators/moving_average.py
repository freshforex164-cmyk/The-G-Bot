"""Simple and exponential moving averages over closed-candle closes."""

from __future__ import annotations

from collections.abc import Sequence

from config import MOVING_AVERAGE_METHOD, MOVING_AVERAGE_PERIOD
from data.candles import Candle


def calculate_moving_average(
    candles: Sequence[Candle],
    period: int = MOVING_AVERAGE_PERIOD,
    method: str = MOVING_AVERAGE_METHOD,
) -> list[float | None]:
    """Return an aligned SMA or EMA series; leading values are None until ready."""
    if period <= 0:
        raise ValueError("period must be greater than zero")

    method = method.upper()
    if method not in {"SMA", "EMA"}:
        raise ValueError("method must be 'SMA' or 'EMA'")

    closes = [float(candle.close) for candle in candles]
    values: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return values

    initial_average = sum(closes[:period]) / period
    values[period - 1] = initial_average

    if method == "SMA":
        rolling_sum = sum(closes[:period])
        for index in range(period, len(closes)):
            rolling_sum += closes[index] - closes[index - period]
            values[index] = rolling_sum / period
        return values

    multiplier = 2.0 / (period + 1)
    previous = initial_average
    for index in range(period, len(closes)):
        previous = (closes[index] - previous) * multiplier + previous
        values[index] = previous
    return values
