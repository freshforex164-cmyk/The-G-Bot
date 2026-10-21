"""Closed-candle pullback/reversal signals with touch and rejection triggers."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import config
from data.candles import Candle
from strategy.rules import DirectionEvaluation, evaluate_direction, trigger_a, trigger_b
from strategy.strength import score_strength


@dataclass
class Signal:
    pair: str
    direction: str
    price: float
    time: datetime
    stochastic_k: float | None
    stochastic_d: float | None
    ma_slope: float | None
    bollinger_middle: float
    id: str = field(default_factory=lambda: uuid4().hex)
    payout: float = 0.0
    strength: int = 0
    strength_components: dict[str, int] = field(default_factory=dict)
    recommended_expiry_seconds: int = config.RECOMMENDED_EXPIRY_SECONDS
    valid_until: datetime | None = None
    status: str = "ACTIVE"

    @property
    def entry_price(self) -> float:
        return self.price


@dataclass
class _PairState:
    last_closed_time: int | None = None
    cooldown_remaining: int = 0
    pending_setup: DirectionEvaluation | None = None
    closed_signal: Signal | None = None
    forming_time: int | None = None


class PullbackReversalStrategy:
    """Evaluate S1–S4 on closed candles; emit information-only signals."""

    def __init__(self) -> None:
        if config.TRIGGER_VARIANT not in {"A", "B"}:
            raise ValueError("TRIGGER_VARIANT must be 'A' or 'B'")
        self._states: dict[str, _PairState] = {}

    def on_closed_candles(
        self, pair: str, candles: Sequence[Candle], *, payout: float = 0.0
    ) -> str | None:
        """Evaluate one new close and return its ready direction, if any."""
        if not candles:
            return None
        state = self._states.setdefault(pair, _PairState())
        latest = candles[-1]
        if state.last_closed_time == latest.time:
            if state.closed_signal is not None:
                return state.closed_signal.direction
            return state.pending_setup.direction if state.pending_setup else None

        state.last_closed_time = latest.time
        state.pending_setup = None
        state.closed_signal = None
        if state.cooldown_remaining > 0:
            state.cooldown_remaining -= 1
        if state.cooldown_remaining > 0:
            return None

        if config.TRIGGER_VARIANT == "B":
            if len(candles) < 2:
                return None
            prior_candles = candles[:-1]
            for direction in ("SELL", "BUY"):
                evaluation = evaluate_direction(prior_candles, direction)
                triggered, _detail = trigger_b(evaluation, latest)
                if triggered:
                    self._states[pair].closed_signal = self._make_signal(
                        pair,
                        evaluation,
                        float(latest.close),
                        datetime.fromtimestamp(latest.time, timezone.utc),
                        payout,
                        touch_cleanliness=1.0,
                    )
                    state.cooldown_remaining = config.SIGNAL_COOLDOWN_CANDLES
                    return direction
            return None

        ready = [
            evaluate_direction(candles, direction)
            for direction in ("SELL", "BUY")
        ]
        ready = [evaluation for evaluation in ready if evaluation.setup_passes]
        if not ready:
            return None
        state.pending_setup = max(
            ready,
            key=lambda evaluation: self._score(evaluation, 0.0)[0],
        )
        return state.pending_setup.direction

    def _find_setup(
        self, candles: Sequence[Candle]
    ) -> DirectionEvaluation | None:
        """Return the first matching side, useful to replay and diagnostics."""
        for direction in ("SELL", "BUY"):
            evaluation = evaluate_direction(candles, direction)
            if evaluation.setup_passes:
                return evaluation
        return None

    def take_closed_signal(self, pair: str) -> Signal | None:
        """Consume a signal created by the closed-candle rejection trigger."""
        state = self._states.setdefault(pair, _PairState())
        signal = state.closed_signal
        state.closed_signal = None
        return signal

    @staticmethod
    def _score(
        evaluation: DirectionEvaluation, touch_cleanliness: float
    ) -> tuple[int, dict[str, int]]:
        return score_strength(
            stochastic_extremity=evaluation.stochastic_extremity,
            stochastic_turn=evaluation.stochastic_turn,
            moving_average_turn=evaluation.ma_turn_percent,
            bollinger_slope=evaluation.middle_slope_percent,
            bollinger_curvature=evaluation.band_curvature_percent,
            touch_cleanliness=touch_cleanliness,
        )

    def _make_signal(
        self,
        pair: str,
        evaluation: DirectionEvaluation,
        price: float,
        timestamp: datetime,
        payout: float,
        *,
        touch_cleanliness: float,
    ) -> Signal:
        strength, components = self._score(evaluation, touch_cleanliness)
        return Signal(
            pair=pair,
            direction=evaluation.direction,
            price=price,
            time=timestamp,
            stochastic_k=evaluation.stochastic_k,
            stochastic_d=evaluation.stochastic_d,
            ma_slope=evaluation.ma_slope_3,
            bollinger_middle=evaluation.middle if evaluation.middle is not None else price,
            payout=float(payout),
            strength=strength,
            strength_components=components,
            recommended_expiry_seconds=config.RECOMMENDED_EXPIRY_SECONDS,
            valid_until=timestamp + timedelta(seconds=config.SIGNAL_VALID_SECONDS),
        )

    def on_tick(
        self,
        pair: str,
        live_price: float,
        forming_candle: Candle | None,
        *,
        timestamp: datetime | None = None,
        payout: float = 0.0,
    ) -> Signal | None:
        """Apply trigger A to the current forming candle and closed setup."""
        if config.TRIGGER_VARIANT != "A" or forming_candle is None:
            return None
        try:
            price = float(live_price)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price):
            return None

        state = self._states.setdefault(pair, _PairState())
        state.forming_time = forming_candle.time
        evaluation = state.pending_setup
        if evaluation is None:
            return None
        triggered, _detail = trigger_a(evaluation, price, forming_candle)
        if not triggered:
            return None

        candle_range = max(
            0.0, float(forming_candle.high) - float(forming_candle.low)
        )
        reference = candle_range * config.STRENGTH_TOUCH_REFERENCE_RANGE_MULTIPLIER
        cleanliness = (
            max(
                0.0,
                1.0 - abs(price - (evaluation.middle or price)) / reference,
            )
            if reference > 0
            else 0.0
        )
        signal_time = timestamp or datetime.now(timezone.utc)
        signal = self._make_signal(
            pair,
            evaluation,
            price,
            signal_time,
            payout,
            touch_cleanliness=cleanliness,
        )
        state.pending_setup = None
        state.cooldown_remaining = config.SIGNAL_COOLDOWN_CANDLES
        return signal
