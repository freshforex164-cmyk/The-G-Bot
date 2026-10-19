"""Slow stochastic oscillator (%K and %D) over closed OHLC candles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from config import STOCHASTIC_PERIOD, STOCHASTIC_SMOOTH_D, STOCHASTIC_SMOOTH_K
from data.candles import Candle


@dataclass(frozen=True)
class StochasticSeries:
    slow_k: list[float | None]
    slow_d: list[float | None]


def _aligned_sma(values: list[float | None], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if all(value is not None for value in window):
            result[index] = sum(value for value in window if value is not None) / period
    return result


def calculate_stochastic(
    candles: Sequence[Candle],
    period: int = STOCHASTIC_PERIOD,
    smooth_k: int = STOCHASTIC_SMOOTH_K,
    smooth_d: int = STOCHASTIC_SMOOTH_D,
) -> StochasticSeries:
    """Return slow %K and %D aligned to candle indexes.

    A zero-range lookback maps to neutral raw %K = 50 rather than dividing by zero.
    """
    if min(period, smooth_k, smooth_d) <= 0:
        raise ValueError("period and smoothing values must be greater than zero")

    raw_k: list[float | None] = [None] * len(candles)
    for index in range(period - 1, len(candles)):
        window = candles[index - period + 1 : index + 1]
        highest = max(float(candle.high) for candle in window)
        lowest = min(float(candle.low) for candle in window)
        close = float(candles[index].close)
        width = highest - lowest
        raw_k[index] = 50.0 if width == 0 else (close - lowest) / width * 100.0

    slow_k = _aligned_sma(raw_k, smooth_k)
    slow_d = _aligned_sma(slow_k, smooth_d)
    return StochasticSeries(slow_k=slow_k, slow_d=slow_d)
