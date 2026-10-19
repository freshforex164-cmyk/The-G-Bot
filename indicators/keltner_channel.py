"""Keltner Channel using an EMA middle and Wilder ATR."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config import KELTNER_ATR_MULTIPLIER, KELTNER_ATR_PERIOD, KELTNER_EMA_PERIOD
from data.candles import Candle
from indicators.moving_average import calculate_moving_average


@dataclass(frozen=True)
class KeltnerChannel:
    middle: list[float | None]
    upper: list[float | None]
    lower: list[float | None]
    atr: list[float | None]


def calculate_atr(
    candles: Sequence[Candle], period: int = KELTNER_ATR_PERIOD
) -> list[float | None]:
    """Calculate Wilder's ATR, seeded with the first period's simple mean TR."""
    if period <= 0:
        raise ValueError("period must be greater than zero")

    true_ranges: list[float] = []
    for index, candle in enumerate(candles):
        high = float(candle.high)
        low = float(candle.low)
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = float(candles[index - 1].close)
            true_ranges.append(
                max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            )

    values: list[float | None] = [None] * len(true_ranges)
    if len(true_ranges) < period:
        return values

    previous_atr = sum(true_ranges[:period]) / period
    values[period - 1] = previous_atr
    for index in range(period, len(true_ranges)):
        previous_atr = (
            previous_atr * (period - 1) + true_ranges[index]
        ) / period
        values[index] = previous_atr
    return values


def calculate_keltner_channel(
    candles: Sequence[Candle],
    ema_period: int = KELTNER_EMA_PERIOD,
    atr_period: int = KELTNER_ATR_PERIOD,
    multiplier: float = KELTNER_ATR_MULTIPLIER,
) -> KeltnerChannel:
    """Calculate Keltner middle/upper/lower bands and the underlying ATR series."""
    if multiplier < 0:
        raise ValueError("multiplier cannot be negative")

    middle = calculate_moving_average(candles, ema_period, "EMA")
    atr_values = calculate_atr(candles, atr_period)
    upper: list[float | None] = [None] * len(candles)
    lower: list[float | None] = [None] * len(candles)

    for index, (ema_value, atr_value) in enumerate(zip(middle, atr_values)):
        if ema_value is None or atr_value is None:
            continue
        upper[index] = ema_value + multiplier * atr_value
        lower[index] = ema_value - multiplier * atr_value

    return KeltnerChannel(
        middle=middle,
        upper=upper,
        lower=lower,
        atr=atr_values,
    )
