"""Candle-only approximate replay and strategy-variant comparison."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import product
from typing import Iterable, Mapping

import config
from data.candles import Candle
from strategy.rules import (
    DirectionEvaluation,
    IndicatorSet,
    evaluate_direction,
    prepare_indicators,
    trigger_a,
    trigger_b,
)
from strategy.strength import score_strength

REPLAY_HORIZONS = (3, 6, 12)


@dataclass(frozen=True)
class ReplaySignal:
    pair: str
    direction: str
    candle_index: int
    entry_price: float
    strength: int
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


@dataclass(frozen=True)
class ReplayVariant:
    name: str
    trigger: str
    s2_enabled: bool
    s4_enabled: bool
    stochastic_lookback: int


def strategy_variants() -> list[ReplayVariant]:
    return [
        ReplayVariant(
            name=f"T{trigger}-S2{'on' if s2 else 'off'}-S4{'on' if s4 else 'off'}-K{lookback}",
            trigger=trigger,
            s2_enabled=s2,
            s4_enabled=s4,
            stochastic_lookback=lookback,
        )
        for trigger, s2, s4, lookback in product(
            ("A", "B"),
            (True, False),
            (True, False),
            config.STOCHASTIC_LOOKBACK_OPTIONS,
        )
    ]


def _approximate_trigger(
    evaluation: DirectionEvaluation, candle: Candle, trigger: str
) -> tuple[bool, float]:
    if evaluation.middle is None:
        return False, float(candle.close)
    if trigger == "B":
        matched, _ = trigger_b(evaluation, candle)
        return matched, float(candle.close)

    # OHLC replay cannot know the exact tick path; use the first plausible
    # in-zone price reached after the candle open and label results approximate.
    epsilon = max(abs(evaluation.middle) * 1e-10, 1e-10)
    if evaluation.direction == "SELL":
        price = max(evaluation.middle, float(candle.open) + epsilon)
        possible = candle.high >= price and price <= (evaluation.upper or price)
    else:
        price = min(evaluation.middle, float(candle.open) - epsilon)
        possible = candle.low <= price and price >= (evaluation.lower or price)
    if not possible:
        return False, price
    matched, _ = trigger_a(evaluation, price, candle)
    return matched, price


def _strength(evaluation: DirectionEvaluation, price: float) -> int:
    distance = abs(price - (evaluation.middle or price))
    reference = max(abs(evaluation.middle or price) * 0.001, 1e-12)
    touch_cleanliness = max(0.0, 1.0 - distance / reference)
    score, _ = score_strength(
        stochastic_extremity=evaluation.stochastic_extremity,
        stochastic_turn=evaluation.stochastic_turn,
        moving_average_turn=evaluation.ma_turn_percent,
        bollinger_slope=evaluation.middle_slope_percent,
        bollinger_curvature=evaluation.band_curvature_percent,
        touch_cleanliness=touch_cleanliness,
    )
    return score


def replay_variant(
    pair: str,
    candles: Iterable[Candle],
    variant: ReplayVariant,
    *,
    indicators: IndicatorSet | None = None,
) -> ReplaySummary:
    """Replay one strategy configuration over a shared closed-candle series."""
    series = list(candles)
    summary = ReplaySummary(pair=pair, signals=[])
    prepared = indicators or prepare_indicators(series)
    enabled = {
        "S1": config.S1_ENABLED,
        "S2": config.S2_ENABLED and variant.s2_enabled,
        "S3": config.S3_ENABLED,
        "S4": config.S4_ENABLED and variant.s4_enabled,
    }
    last_signal_index = -config.SIGNAL_COOLDOWN_CANDLES - 1

    for current_index in range(1, len(series)):
        if current_index - last_signal_index <= config.SIGNAL_COOLDOWN_CANDLES:
            continue
        setup_index = current_index - 1
        candidates: list[tuple[DirectionEvaluation, float]] = []
        for direction in ("SELL", "BUY"):
            evaluation = evaluate_direction(
                series,
                direction,
                stochastic_lookback=variant.stochastic_lookback,
                enabled=enabled,
                index=setup_index,
                indicators=prepared,
            )
            if not evaluation.setup_passes:
                continue
            triggered, entry = _approximate_trigger(
                evaluation, series[current_index], variant.trigger
            )
            if triggered:
                candidates.append((evaluation, entry))
        if not candidates:
            continue

        evaluation, entry_price = max(
            candidates, key=lambda item: _strength(item[0], item[1])
        )
        outcomes: dict[int, str] = {}
        for horizon in REPLAY_HORIZONS:
            exit_index = current_index + horizon
            if exit_index < len(series):
                exit_price = float(series[exit_index].close)
                favorable = (
                    exit_price < entry_price
                    if evaluation.direction == "SELL"
                    else exit_price > entry_price
                )
                outcomes[horizon] = "WIN" if favorable else "LOSS"
        signal = ReplaySignal(
            pair=pair,
            direction=evaluation.direction,
            candle_index=current_index,
            entry_price=entry_price,
            strength=_strength(evaluation, entry_price),
            outcomes=outcomes,
        )
        summary.signals.append(signal)
        summary.directions[evaluation.direction] += 1
        bucket = (
            "0-59" if signal.strength < 60 else "60-79" if signal.strength < 80 else "80-100"
        )
        summary.strength_buckets[bucket] += 1
        for horizon, result in outcomes.items():
            summary.outcomes[horizon][result] += 1
        last_signal_index = current_index
    return summary


def replay_candles(pair: str, candles: Iterable[Candle]) -> ReplaySummary:
    """Backwards-compatible default replay summary, with approximate OHLC fills."""
    variant = next(
        variant
        for variant in strategy_variants()
        if variant.trigger == config.TRIGGER_VARIANT
        and variant.s2_enabled == config.S2_ENABLED
        and variant.s4_enabled == config.S4_ENABLED
        and variant.stochastic_lookback == config.STOCHASTIC_LOOKBACK_CANDLES
    )
    return replay_variant(pair, candles, variant)


def run_strategy_lab(
    candles_by_pair: Mapping[str, Iterable[Candle]],
) -> dict[str, ReplaySummary]:
    """Compare all requested rule variants against the same candle histories."""
    variants = strategy_variants()
    results = {variant.name: ReplaySummary("TOTAL", []) for variant in variants}
    for pair, candles in candles_by_pair.items():
        series = list(candles)
        prepared = prepare_indicators(series)
        for variant in variants:
            item = replay_variant(pair, series, variant, indicators=prepared)
            total = results[variant.name]
            total.signals.extend(item.signals)
            total.directions.update(item.directions)
            total.strength_buckets.update(item.strength_buckets)
            for horizon in REPLAY_HORIZONS:
                total.outcomes[horizon].update(item.outcomes[horizon])
    return results


def win_rate(summary: ReplaySummary, horizon: int) -> float | None:
    outcomes = summary.outcomes[horizon]
    count = outcomes["WIN"] + outcomes["LOSS"]
    return outcomes["WIN"] / count * 100.0 if count else None


def strength_percentiles(summaries: Iterable[ReplaySummary]) -> dict[int, float] | None:
    values = sorted(
        signal.strength
        for summary in summaries
        for signal in summary.signals
    )
    if not values:
        return None

    def percentile(p: int) -> float:
        if len(values) == 1:
            return float(values[0])
        position = (len(values) - 1) * p / 100
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        fraction = position - lower
        return values[lower] * (1 - fraction) + values[upper] * fraction

    return {p: percentile(p) for p in (10, 50, 90)}
