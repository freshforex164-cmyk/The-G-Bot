"""Closed-candle Stochastic/Bollinger pullback-rejection state machine."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

import config
from data.candles import Candle
from indicators.atr import calculate_atr
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic
from strategy.pullback_reversal import Signal


@dataclass
class DirectionState:
    status: str = "IDLE"
    reason: str = ""
    arm_index: int | None = None
    arm_time: int | None = None
    peak_or_trough: float | None = None
    drop: float | None = None


@dataclass(frozen=True)
class TriggerCheck:
    middle: bool
    lower: bool
    middle_reason: str
    lower_reason: str
    wick_ratio: float
    overshoot_atr: float
    candle_range: float
    body: float


def arm_measurements(
    direction: str, latest: float, previous: float, older: float
) -> tuple[bool, float, float, str]:
    """Return S1 pass, extreme, pullback, and first failing rule."""
    if direction == "SELL":
        extreme = max(latest, previous, older)
        drop = extreme - latest
        checks = (
            (extreme >= config.STOCHASTIC_OVERBOUGHT, "peak below overbought"),
            (config.PULLBACK_MIN_DROP <= drop <= config.PULLBACK_MAX_DROP, "drop outside range"),
            (latest < previous, "Stochastic did not turn down"),
            (latest >= config.SELL_STOCHASTIC_FLOOR, "K below sell floor"),
        )
    else:
        extreme = min(latest, previous, older)
        drop = latest - extreme
        checks = (
            (extreme <= config.STOCHASTIC_OVERSOLD, "trough above oversold"),
            (config.PULLBACK_MIN_DROP <= drop <= config.PULLBACK_MAX_DROP, "rise outside range"),
            (latest > previous, "Stochastic did not turn up"),
            (latest <= config.BUY_STOCHASTIC_CEILING, "K above buy ceiling"),
        )
    failure = next((reason for passed, reason in checks if not passed), "")
    return not failure, extreme, drop, failure


def trigger_measurements(
    direction: str,
    candle: Candle,
    *,
    atr: float,
    middle: float,
    upper: float,
    lower_band: float,
) -> TriggerCheck:
    """Evaluate the two configured close-only rejection trigger types."""
    o, h, low, close = map(
        float, (candle.open, candle.high, candle.low, candle.close)
    )
    candle_range = max(0.0, h - low)
    body = abs(close - o)
    eps = config.EPS_ATR_RATIO * atr
    body_ok = body <= config.MAX_BODY_ATR_RATIO * atr
    small = body_ok and (close > o if direction == "SELL" else close < o)
    wick = (h - max(o, close)) if direction == "SELL" else (min(o, close) - low)
    wick_ratio = wick / candle_range if candle_range > 0 else 0.0
    if direction == "SELL":
        middle_touch = low - eps <= middle <= h + eps
        middle_overshoot = max(0.0, h - middle)
        middle = (
            config.MIDDLE_ENABLED
            and small
            and middle_touch
            and close < middle
            and wick_ratio >= config.MIN_REJECTION_WICK_RATIO
            and middle_overshoot <= config.MAX_TOUCH_ATR * atr
        )
        lower = (
            config.LOWER_ENABLED
            and small
            and low - eps <= lower_band <= h + eps
            and close < middle
        )
        over = middle_overshoot / atr if atr > 0 else math.inf
    else:
        middle_touch = low - eps <= middle <= h + eps
        middle_overshoot = max(0.0, middle - low)
        middle = (
            config.MIDDLE_ENABLED
            and small
            and middle_touch
            and close > middle
            and wick_ratio >= config.MIN_REJECTION_WICK_RATIO
            and middle_overshoot <= config.MAX_TOUCH_ATR * atr
        )
        upper_touch = low - eps <= upper <= h + eps
        lower = (
            config.LOWER_ENABLED
            and small
            and upper_touch
            and close > middle
        )
        over = middle_overshoot / atr if atr > 0 else math.inf
    common = (
        "small directional candle"
        if small
        else "candle direction/body failed"
    )
    return TriggerCheck(
        middle=middle,
        lower=lower,
        middle_reason="middle rejection passed" if middle else common,
        lower_reason="outer band touch passed" if lower else common,
        wick_ratio=wick_ratio,
        overshoot_atr=over,
        candle_range=candle_range,
        body=body,
    )


class CloseOnlyStrategy:
    """One-fire-per-setup state machine; all decisions use completed candles."""

    def __init__(self) -> None:
        self.states: dict[tuple[str, str], DirectionState] = {}
        self._last_time: dict[str, int] = {}
        self._cooldown: dict[str, int] = {}
        self._closed_signals: dict[str, Signal] = {}

    def state(self, pair: str, direction: str) -> DirectionState:
        return self.states.setdefault((pair, direction), DirectionState())

    def on_closed_candles(
        self, pair: str, candles: Sequence[Candle], *, payout: float = 0.0
    ) -> str | None:
        if len(candles) < config.MIN_STRATEGY_CANDLES:
            return None
        latest = candles[-1]
        if self._last_time.get(pair) == latest.time:
            signal = self._closed_signals.get(pair)
            return signal.direction if signal else None
        self._last_time[pair] = latest.time
        self._closed_signals.pop(pair, None)
        index = len(candles) - 1
        stochastic = calculate_stochastic(candles)
        bands = calculate_bollinger_bands(candles)
        moving_average = calculate_moving_average(
            candles,
            period=config.MOVING_AVERAGE_PERIOD,
            method=config.MOVING_AVERAGE_METHOD,
        )
        atr_values = calculate_atr(candles)
        atr = atr_values[-1]
        k = stochastic.slow_k
        if (
            atr is None
            or atr <= 0
            or any(k[i] is None for i in (index, index - 1, index - 2))
            or any(
                series[index] is None
                for series in (bands.middle, bands.upper, bands.lower)
            )
        ):
            self._cancel_armed(pair, "indicator history unavailable or ATR is flat")
            return None
        k0, k1, k2 = (float(k[i]) for i in (index, index - 1, index - 2))
        mid = float(bands.middle[index])
        up = float(bands.upper[index])
        lo = float(bands.lower[index])
        fired: list[tuple[Signal, str]] = []
        cooldown = self._cooldown.get(pair, 0)
        if cooldown > 0:
            self._cooldown[pair] = cooldown - 1
            for direction in ("SELL", "BUY"):
                state = self.state(pair, direction)
                state.status, state.reason = "IDLE", "pair cooldown"
                state.arm_index = None
            return None

        for direction in ("SELL", "BUY"):
            state = self.state(pair, direction)
            if state.status in {"FIRE", "CANCEL"}:
                state.status, state.reason = "IDLE", ""
                state.arm_index = None
            if state.status == "ARMED":
                bars_after = index - int(state.arm_index or 0)
                peak = float(state.peak_or_trough or 0.0)
                close = float(latest.close)
                cancel_reason = ""
                if direction == "SELL":
                    if k0 >= peak:
                        cancel_reason = "K returned to setup peak"
                    elif k0 < config.SELL_STOCHASTIC_FLOOR:
                        cancel_reason = "K fell below sell floor"
                    elif close > up:
                        cancel_reason = "close above upper band"
                else:
                    if k0 <= peak:
                        cancel_reason = "K returned to setup trough"
                    elif k0 > config.BUY_STOCHASTIC_CEILING:
                        cancel_reason = "K rose above buy ceiling"
                    elif close < lo:
                        cancel_reason = "close below lower band"
                if not cancel_reason and bars_after > config.SETUP_WINDOW_CANDLES:
                    cancel_reason = "setup window expired"
                if cancel_reason:
                    state.status, state.reason = "CANCEL", cancel_reason
                    state.arm_index = None
                    continue
                check = trigger_measurements(
                    direction,
                    latest,
                    atr=float(atr),
                    middle=mid,
                    upper=up,
                    lower=lo,
                )
                trigger_type = (
                    "MIDDLE"
                    if check.middle
                    else "LOWER"
                    if check.lower
                    else ""
                )
                if trigger_type and config.S1_ENABLED:
                    signal = self._make_signal(
                        pair,
                        direction,
                        latest,
                        payout,
                        float(atr),
                        k0,
                        float(state.drop or 0.0),
                        trigger_type,
                        check,
                        moving_average,
                        index,
                        bars_after,
                        mid,
                        float(stochastic.slow_d[index])
                        if stochastic.slow_d[index] is not None
                        else None,
                    )
                    state.status, state.reason = "FIRE", trigger_type
                    state.arm_index = None
                    fired.append((signal, trigger_type))
                    continue
                state.reason = (
                    f"ARMED bars={bars_after}/{config.SETUP_WINDOW_CANDLES}; "
                    f"middle={check.middle_reason}; lower={check.lower_reason}"
                )
                continue

            if not config.S1_ENABLED:
                state.status, state.reason = "IDLE", "S1 disabled"
                continue
            passed, extreme, drop, failure = arm_measurements(direction, k0, k1, k2)
            if passed:
                state.status = "ARMED"
                state.reason = "S1 armed"
                state.arm_index = index
                state.arm_time = latest.time
                state.peak_or_trough = extreme
                state.drop = drop
            else:
                state.status, state.reason = "IDLE", failure

        if not fired:
            return None
        signal, _trigger_type = max(fired, key=lambda item: item[0].strength)
        self._closed_signals[pair] = signal
        self._cooldown[pair] = config.SIGNAL_COOLDOWN_CANDLES
        for direction in ("SELL", "BUY"):
            if direction != signal.direction:
                state = self.state(pair, direction)
                if state.status == "ARMED":
                    state.status, state.reason = "CANCEL", "opposite direction fired"
                    state.arm_index = None
        return signal.direction

    def take_closed_signal(self, pair: str) -> Signal | None:
        return self._closed_signals.pop(pair, None)

    def on_tick(self, *args: object, **kwargs: object) -> None:
        """Intrabar ticks are intentionally ignored."""
        return None

    def _cancel_armed(self, pair: str, reason: str) -> None:
        for direction in ("SELL", "BUY"):
            state = self.state(pair, direction)
            if state.status == "ARMED":
                state.status, state.reason, state.arm_index = "CANCEL", reason, None

    @staticmethod
    def _make_signal(
        pair: str,
        direction: str,
        candle: Candle,
        payout: float,
        atr: float,
        k: float,
        drop: float,
        trigger_type: str,
        check: TriggerCheck,
        moving_average: Sequence[float | None],
        index: int,
        bars_after: int,
        middle: float,
        stochastic_d: float | None,
    ) -> Signal:
        score, components = score_close_strength(
            direction=direction,
            drop=drop,
            trigger_type=trigger_type,
            wick_ratio=check.wick_ratio,
            overshoot_atr=check.overshoot_atr,
            moving_average=moving_average,
            index=index,
            bars_after=bars_after,
            atr=atr,
        )
        time = datetime.fromtimestamp(candle.time, timezone.utc)
        return Signal(
            pair=pair,
            direction=direction,
            price=float(candle.close),
            time=time,
            stochastic_k=k,
            stochastic_d=stochastic_d,
            ma_slope=(
                (float(moving_average[index]) - float(moving_average[index - 3])) / atr
                if index >= 3
                and moving_average[index] is not None
                and moving_average[index - 3] is not None
                else None
            ),
            bollinger_middle=middle,
            payout=payout,
            strength=score,
            strength_components=components,
            recommended_expiry_seconds=config.EXPIRATION_SECONDS,
            valid_until=time + timedelta(seconds=config.SIGNAL_VALID_SECONDS),
            trigger_type=trigger_type,
        )


def score_close_strength(
    *,
    direction: str,
    drop: float,
    trigger_type: str,
    wick_ratio: float,
    overshoot_atr: float,
    moving_average: Sequence[float | None],
    index: int,
    bars_after: int,
    atr: float,
) -> tuple[int, dict[str, int]]:
    drop_quality = max(
        0.0,
        1.0 - abs(drop - config.STRENGTH_TARGET_DROP) / config.STRENGTH_DROP_RADIUS,
    )
    wick_quality = min(
        1.0,
        max(0.0, (wick_ratio - config.MIN_REJECTION_WICK_RATIO)
            / (config.STRENGTH_WICK_FULL_SCORE_RATIO - config.MIN_REJECTION_WICK_RATIO)),
    )
    overshoot_quality = max(
        0.0, 1.0 - overshoot_atr / config.MAX_TOUCH_ATR
    )
    rejection_quality = (wick_quality + overshoot_quality) / 2
    touch_quality = (
        1.0 if trigger_type == "MIDDLE" else config.STRENGTH_LOWER_TOUCH_MULTIPLIER
    )
    ma0 = moving_average[index] if index < len(moving_average) else None
    ma3 = moving_average[index - 3] if index >= 3 else None
    slope = (
        (float(ma0) - float(ma3)) / atr
        if ma0 is not None and ma3 is not None
        else 0.0
    )
    aligned = slope <= 0 if direction == "SELL" else slope >= 0
    ma_quality = 1.0 if aligned else 0.0
    timing_quality = max(
        0.0, (config.SETUP_WINDOW_CANDLES + 1 - bars_after)
        / config.SETUP_WINDOW_CANDLES,
    )
    raw = {
        "stochastic": drop_quality * config.STRENGTH_WEIGHT_STOCHASTIC,
        "rejection": rejection_quality * config.STRENGTH_WEIGHT_REJECTION,
        "touch": touch_quality * config.STRENGTH_WEIGHT_TOUCH,
        "moving_average": ma_quality * config.STRENGTH_WEIGHT_MA,
        "timing": timing_quality * config.STRENGTH_WEIGHT_TIMING,
    }
    components = {name: round(value) for name, value in raw.items()}
    return max(0, min(100, round(sum(raw.values())))), components


def explain_close_state(
    pair: str, candles: Sequence[Candle], strategy: CloseOnlyStrategy
) -> list[str]:
    """Return numeric latest-candle rule diagnostics and the first failure."""
    lines = [f"EXPLAIN {pair}"]
    if len(candles) < config.MIN_STRATEGY_CANDLES:
        return lines + [f"history={len(candles)} required={config.MIN_STRATEGY_CANDLES} ✗"]
    stochastic = calculate_stochastic(candles)
    bands = calculate_bollinger_bands(candles)
    ma = calculate_moving_average(
        candles, config.MOVING_AVERAGE_PERIOD, config.MOVING_AVERAGE_METHOD
    )
    atr = calculate_atr(candles)[-1]
    index = len(candles) - 1
    k0, k1, k2 = (stochastic.slow_k[i] for i in (index, index - 1, index - 2))
    if atr is None or atr <= 0 or any(value is None for value in (k0, k1, k2)):
        return lines + ["ATR/Stochastic unavailable or flat ✗"]
    for direction in ("SELL", "BUY"):
        passed, extreme, drop, failure = arm_measurements(
            direction, float(k0), float(k1), float(k2)
        )
        state = strategy.state(pair, direction)
        lines.append(
            f"{direction} S1 {'✓' if passed else '✗'} K={float(k0):.2f}/"
            f"{float(k1):.2f}/{float(k2):.2f} extreme={extreme:.2f} "
            f"drop={drop:.2f}; state={state.status} {state.reason}"
        )
        if not passed:
            lines.append(f"  first failure: {failure}")
        if bands.middle[index] is None:
            continue
        check = trigger_measurements(
            direction,
            candles[-1],
            atr=float(atr),
            middle=float(bands.middle[index]),
            upper=float(bands.upper[index]),
            lower=float(bands.lower[index]),
        )
        lines.append(
            f"  MIDDLE {'✓' if check.middle else '✗'} wick={check.wick_ratio:.3f} "
            f"overshootATR={check.overshoot_atr:.3f} body={check.body:.6g} "
            f"ATR={float(atr):.6g}; {check.middle_reason}"
        )
        lines.append(
            f"  LOWER {'✓' if check.lower else '✗'} range={check.candle_range:.6g}; "
            f"{check.lower_reason}"
        )
    return lines
