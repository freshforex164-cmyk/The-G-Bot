"""Bollinger Bands using population standard deviation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from config import BOLLINGER_PERIOD, BOLLINGER_STANDARD_DEVIATIONS
from data.candles import Candle


@dataclass(frozen=True)
class BollingerBandSeries:
    middle: list[float | None]
    upper: list[float | None]
    lower: list[float | None]


def calculate_bollinger_bands(
    candles: Sequence[Candle],
    period: int = BOLLINGER_PERIOD,
    standard_deviations: float = BOLLINGER_STANDARD_DEVIATIONS,
) -> BollingerBandSeries:
    """Calculate aligned middle/upper/lower series from closed-candle closes."""
    if period <= 0:
        raise ValueError("period must be greater than zero")
    if standard_deviations < 0:
        raise ValueError("standard_deviations cannot be negative")

    closes = [float(candle.close) for candle in candles]
    middle: list[float | None] = [None] * len(closes)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)

    for index in range(period - 1, len(closes)):
        window = closes[index - period + 1 : index + 1]
        average = sum(window) / period
        variance = sum((close - average) ** 2 for close in window) / period
        deviation = math.sqrt(variance)
        middle[index] = average
        upper[index] = average + standard_deviations * deviation
        lower[index] = average - standard_deviations * deviation

    return BollingerBandSeries(middle=middle, upper=upper, lower=lower)
