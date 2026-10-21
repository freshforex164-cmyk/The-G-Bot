"""Candle-only approximate replay for historical strategy signals."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from config import SIGNAL_COOLDOWN_CANDLES
from data.candles import Candle
from indicators.bollinger_bands import calculate_bollinger_bands
from strategy.pullback_reversal import PullbackReversalStrategy

REPLAY_HORIZONS = (3, 6, 12)


@dataclass(frozen=True)
class ReplaySignal:
    pair: str
    direction: str
    candle_index: int
    entry_price: float
    strength: float | None = None
    outcomes: dict[int, str] = field(default_factory=dict)


@dataclass
class ReplaySummary:
    pair: str
    signals: list[ReplaySignal]
    directions: Counter[str] = field(default_factory=Counter)
    strength_buckets: Counter[str] = field(default_factory=Counter)
    outcomes: dict[int, Counter[str]] = field(
        default_factory=lambda: {horizon: Counter() for horizon in REPLAY_HORIZONS}
    )

    @property
    def signal_count(self) -> int:
        return len(self.signals)


def _outcome(direction: str, entry: float, exit_price: float) -> str:
    favorable = exit_price < entry if direction == "SELL" else exit_price > entry
    return "WIN" if favorable else "LOSS"


def replay_candles(pair: str, candles: Iterable[Candle]) -> ReplaySummary:
    """Replay approximate touches and score at 3, 6, and 12 future candles."""
    series = list(candles)
    summary = ReplaySummary(pair=pair, signals=[])
    strategy = PullbackReversalStrategy()
    last_signal_index = -SIGNAL_COOLDOWN_CANDLES - 1

    for current_index in range(1, len(series)):
        if current_index - last_signal_index <= SIGNAL_COOLDOWN_CANDLES:
            continue
        setup = strategy._find_setup(series[:current_index])
        if setup is None:
            continue

        current = series[current_index]
        band_series = calculate_bollinger_bands((*series[:current_index], current))
        middle = band_series.middle[-1]
        if middle is None:
            continue
        bullish = current.close > current.open
        bearish = current.close < current.open
        touched = (
            current.high >= middle and bullish
            if setup.direction == "SELL"
            else current.low <= middle and bearish
        )
        if not touched:
            continue

        outcomes: dict[int, str] = {}
        for horizon in REPLAY_HORIZONS:
            exit_index = current_index + horizon
            if exit_index < len(series):
                outcomes[horizon] = _outcome(
                    setup.direction, float(middle), float(series[exit_index].close)
                )

        signal = ReplaySignal(
            pair=pair,
            direction=setup.direction,
            candle_index=current_index,
            entry_price=float(middle),
            outcomes=outcomes,
        )
        summary.signals.append(signal)
        summary.directions[setup.direction] += 1
        summary.strength_buckets["unscored"] += 1
        for horizon, result in outcomes.items():
            summary.outcomes[horizon][result] += 1
        last_signal_index = current_index

    return summary
