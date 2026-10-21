"""Explainable closed-candle setup rules shared by live, replay, and CLI tools."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import config
from data.candles import Candle
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic


@dataclass(frozen=True)
class ConditionResult:
    key: str
    passed: bool
    enabled: bool
    detail: str

    @property
    def eligible(self) -> bool:
        return not self.enabled or self.passed

    def line(self) -> str:
        mark = "✅" if self.passed else "❌"
        if not self.enabled:
            mark = "⏭"
        return f"{self.key} {self.detail} {mark}"


@dataclass(frozen=True)
class DirectionEvaluation:
    direction: str
    conditions: tuple[ConditionResult, ...]
    middle: float | None = None
    upper: float | None = None
    lower: float | None = None
    stochastic_k: float | None = None
    stochastic_d: float | None = None
    ma_slope_3: float | None = None
    stochastic_extremity: float = 0.0
    stochastic_turn: float = 0.0
    ma_turn_percent: float = 0.0
    middle_slope_percent: float = 0.0
    band_curvature_percent: float = 0.0

    @property
    def setup_passes(self) -> bool:
        return all(condition.eligible for condition in self.conditions)

    @property
    def first_failure(self) -> ConditionResult | None:
        return next(
            (condition for condition in self.conditions if not condition.eligible),
            None,
        )


@dataclass(frozen=True)
class IndicatorSet:
    bands: object
    moving_average: list[float | None]
    stochastic: object
    widths: list[float | None]


def _fmt(value: float | None, digits: int = 5) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}"


def _settings(
    enabled: Mapping[str, bool] | None,
    key: str,
    default: bool,
) -> bool:
    return default if enabled is None else enabled.get(key, default)


def prepare_indicators(candles: Sequence[Candle]) -> IndicatorSet:
    bands = calculate_bollinger_bands(candles)
    moving_average = calculate_moving_average(
        candles, period=config.MOVING_AVERAGE_PERIOD, method="EMA"
    )
    stochastic = calculate_stochastic(
        candles,
        period=config.STOCHASTIC_PERIOD,
        smooth_k=config.STOCHASTIC_SMOOTH_K,
        smooth_d=config.STOCHASTIC_SMOOTH_D,
    )
    widths = [
        (upper - lower) / middle
        if middle is not None and middle != 0 and upper is not None and lower is not None
        else None
        for middle, upper, lower in zip(
            bands.middle, bands.upper, bands.lower, strict=True
        )
    ]
    return IndicatorSet(bands, moving_average, stochastic, widths)


def evaluate_direction(
    candles: Sequence[Candle],
    direction: str,
    *,
    stochastic_lookback: int | None = None,
    enabled: Mapping[str, bool] | None = None,
    index: int | None = None,
    indicators: IndicatorSet | None = None,
) -> DirectionEvaluation:
    """Evaluate S1–S4 on chronological input; rule indexes are latest-first."""
    normalized_direction = direction.upper()
    if normalized_direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")

    lookback = (
        config.STOCHASTIC_LOOKBACK_CANDLES
        if stochastic_lookback is None
        else stochastic_lookback
    )
    if lookback <= 0:
        raise ValueError("stochastic lookback must be greater than zero")
    n = len(candles)
    latest = n - 1 if index is None else index
    if latest < 0 or latest >= n:
        raise IndexError("evaluation index must point to a candle in the series")
    s1_on = _settings(enabled, "S1", config.S1_ENABLED)
    s2_on = _settings(enabled, "S2", config.S2_ENABLED)
    s3_on = _settings(enabled, "S3", config.S3_ENABLED)
    s4_on = _settings(enabled, "S4", config.S4_ENABLED)

    prepared = indicators or prepare_indicators(candles)
    bands = prepared.bands
    moving_average = prepared.moving_average
    stochastic = prepared.stochastic
    k0 = stochastic.slow_k[latest] if latest >= 0 else None
    d0 = stochastic.slow_d[latest] if latest >= 0 else None
    k_window = (
        stochastic.slow_k[latest - lookback + 1 : latest + 1]
        if latest + 1 >= lookback
        else []
    )
    max_k = max((value for value in k_window if value is not None), default=None)
    min_k = min((value for value in k_window if value is not None), default=None)
    if normalized_direction == "SELL":
        stochastic_pass = (
            len(k_window) == lookback
            and all(value is not None for value in k_window)
            and max_k is not None
            and k0 is not None
            and d0 is not None
            and max_k >= config.STOCHASTIC_OVERBOUGHT
            and k0 < d0
        )
        stochastic_extremity = (
            max(0.0, max_k - config.STOCHASTIC_OVERBOUGHT)
            if max_k is not None
            else 0.0
        )
        stochastic_turn = max(0.0, d0 - k0) if d0 is not None and k0 is not None else 0.0
    else:
        stochastic_pass = (
            len(k_window) == lookback
            and all(value is not None for value in k_window)
            and min_k is not None
            and k0 is not None
            and d0 is not None
            and min_k <= config.STOCHASTIC_OVERSOLD
            and k0 > d0
        )
        stochastic_extremity = (
            max(0.0, config.STOCHASTIC_OVERSOLD - min_k)
            if min_k is not None
            else 0.0
        )
        stochastic_turn = max(0.0, k0 - d0) if d0 is not None and k0 is not None else 0.0

    if k0 is None or d0 is None or len(k_window) != lookback or any(
        value is None for value in k_window
    ):
        s1_detail = (
            f"Stoch K={_fmt(k0, 1)} D={_fmt(d0, 1)} "
            f"max{lookback}={_fmt(max_k, 1)} min{lookback}={_fmt(min_k, 1)} "
            "(insufficient closed-candle history)"
        )
    else:
        s1_detail = (
            f"Stoch K={_fmt(k0, 1)} D={_fmt(d0, 1)} "
            f"{'max' if normalized_direction == 'SELL' else 'min'}{lookback}="
            f"{_fmt(max_k if normalized_direction == 'SELL' else min_k, 1)} "
            f"({'≥' if normalized_direction == 'SELL' else '≤'}"
            f"{config.STOCHASTIC_OVERBOUGHT if normalized_direction == 'SELL' else config.STOCHASTIC_OVERSOLD:g}; "
            f"K {'<' if normalized_direction == 'SELL' else '>'} D)"
        )

    ma0 = moving_average[latest] if latest >= 0 else None
    ma3 = moving_average[latest - 3] if latest >= 3 else None
    ma6 = moving_average[latest - 6] if latest >= 6 else None
    ma_slope_3 = (
        (ma0 - ma3) / ma0
        if ma0 is not None and ma3 is not None and ma0 != 0
        else None
    )
    prior_ma_slope = (
        (ma3 - ma6) / ma3
        if ma3 is not None and ma6 is not None and ma3 != 0
        else None
    )
    if normalized_direction == "SELL":
        ma_pass = (
            prior_ma_slope is not None
            and ma_slope_3 is not None
            and prior_ma_slope >= config.MA_RISE_MIN
            and ma_slope_3 <= config.MA_FLAT_TOL
        )
        prior_symbol = "≥"
        prior_threshold = config.MA_RISE_MIN
        slope_symbol = "≤"
        slope_threshold = config.MA_FLAT_TOL
    else:
        ma_pass = (
            prior_ma_slope is not None
            and ma_slope_3 is not None
            and prior_ma_slope <= -config.MA_RISE_MIN
            and ma_slope_3 >= -config.MA_FLAT_TOL
        )
        prior_symbol = "≤"
        prior_threshold = -config.MA_RISE_MIN
        slope_symbol = "≥"
        slope_threshold = -config.MA_FLAT_TOL
    ma_detail = (
        f"MA3={_fmt(ma3)} MA6={_fmt(ma6)} prior={_fmt(prior_ma_slope)} "
        f"({prior_symbol}{prior_threshold:+.5f}) slope3={_fmt(ma_slope_3)} "
        f"({slope_symbol}{slope_threshold:+.5f})"
    )

    mid0 = bands.middle[latest] if latest >= 0 else None
    mid3 = bands.middle[latest - 3] if latest >= 3 else None
    up0 = bands.upper[latest] if latest >= 0 else None
    up1 = bands.upper[latest - 1] if latest >= 1 else None
    lo0 = bands.lower[latest] if latest >= 0 else None
    lo1 = bands.lower[latest - 1] if latest >= 1 else None
    close0 = float(candles[latest].close) if latest >= 0 else None
    mid_slope = (
        (mid0 - mid3) / mid0
        if mid0 is not None and mid3 is not None and mid0 != 0
        else None
    )
    if normalized_direction == "SELL":
        structure_pass = (
            mid_slope is not None
            and up0 is not None
            and up1 is not None
            and mid_slope <= config.MID_FLAT_TOL
            and up0 < up1
            and close0 is not None
            and mid0 is not None
            and close0 < mid0
        )
        structure_detail = (
            f"mid0={_fmt(mid0)} mid3={_fmt(mid3)} slope3={_fmt(mid_slope)} "
            f"(≤+{config.MID_FLAT_TOL:.5f}) up0={_fmt(up0)} < up1={_fmt(up1)} "
            f"close0={_fmt(close0)} < mid0"
        )
        curvature = (
            max(0.0, (up1 - (bands.upper[latest - 2] or up1)) - (up0 - up1))
            / mid0
            * 100.0
            if mid0 and up0 is not None and up1 is not None and latest >= 2
            else 0.0
        )
    else:
        structure_pass = (
            mid_slope is not None
            and lo0 is not None
            and lo1 is not None
            and mid_slope >= -config.MID_FLAT_TOL
            and lo0 > lo1
            and close0 is not None
            and mid0 is not None
            and close0 > mid0
        )
        structure_detail = (
            f"mid0={_fmt(mid0)} mid3={_fmt(mid3)} slope3={_fmt(mid_slope)} "
            f"(≥-{config.MID_FLAT_TOL:.5f}) lo0={_fmt(lo0)} > lo1={_fmt(lo1)} "
            f"close0={_fmt(close0)} > mid0"
        )
        curvature = (
            max(0.0, (lo0 - (bands.lower[latest - 2] or lo0)) - (lo1 - lo0))
            / mid0
            * 100.0
            if mid0 and lo0 is not None and lo1 is not None and latest >= 2
            else 0.0
        )

    widths = prepared.widths
    range_window = widths[
        max(0, latest - config.BB_WIDTH_MEAN_LOOKBACK_CANDLES + 1) : latest + 1
    ]
    valid_widths = [width for width in range_window if width is not None]
    width0 = widths[latest] if latest >= 0 else None
    mean_width = (
        sum(valid_widths) / config.BB_WIDTH_MEAN_LOOKBACK_CANDLES
        if len(valid_widths) == config.BB_WIDTH_MEAN_LOOKBACK_CANDLES
        else None
    )
    width_ratio = (
        width0 / mean_width
        if width0 is not None and mean_width is not None and mean_width > 0
        else None
    )
    range_pass = width_ratio is not None and width_ratio <= config.RANGE_MAX_RATIO
    range_detail = (
        f"width0={_fmt(width0, 6)} mean{config.BB_WIDTH_MEAN_LOOKBACK_CANDLES}="
        f"{_fmt(mean_width, 6)} ratio={_fmt(width_ratio, 3)} "
        f"(≤{config.RANGE_MAX_RATIO:g})"
    )
    if mean_width is None:
        range_detail += " (insufficient closed-candle history)"

    conditions = (
        ConditionResult("S1", stochastic_pass, s1_on, s1_detail),
        ConditionResult("S2", ma_pass, s2_on, ma_detail),
        ConditionResult("S3", structure_pass, s3_on, structure_detail),
        ConditionResult("S4", range_pass, s4_on, range_detail),
    )
    return DirectionEvaluation(
        direction=normalized_direction,
        conditions=conditions,
        middle=mid0,
        upper=up0,
        lower=lo0,
        stochastic_k=k0,
        stochastic_d=d0,
        ma_slope_3=ma_slope_3,
        stochastic_extremity=stochastic_extremity,
        stochastic_turn=stochastic_turn,
        ma_turn_percent=abs(ma_slope_3 or 0.0) * 100.0,
        middle_slope_percent=abs(mid_slope or 0.0) * 100.0,
        band_curvature_percent=curvature,
    )


def trigger_a(
    evaluation: DirectionEvaluation,
    live_price: float | None,
    forming_candle: Candle | None,
) -> tuple[bool, str]:
    """Evaluate a live touch in the directional middle-band zone."""
    if not evaluation.setup_passes:
        return False, "setup conditions have not all passed"
    if live_price is None or forming_candle is None or evaluation.middle is None:
        return False, "waiting for a live price and forming candle"
    price = float(live_price)
    bullish = price > float(forming_candle.open)
    bearish = price < float(forming_candle.open)
    if evaluation.direction == "SELL":
        in_zone = evaluation.middle <= price <= (evaluation.upper or evaluation.middle)
        matched = in_zone and bullish
        return (
            matched,
            f"price={_fmt(price)} zone=[{_fmt(evaluation.middle)},"
            f"{_fmt(evaluation.upper)}] bullish={bullish}",
        )
    in_zone = (evaluation.lower or evaluation.middle) <= price <= evaluation.middle
    matched = in_zone and bearish
    return (
        matched,
        f"price={_fmt(price)} zone=[{_fmt(evaluation.lower)},"
        f"{_fmt(evaluation.middle)}] bearish={bearish}",
    )


def trigger_b(
    evaluation: DirectionEvaluation,
    rejection_candle: Candle | None,
) -> tuple[bool, str]:
    """Evaluate the closed-candle rejection against the prior setup's bands."""
    if not evaluation.setup_passes:
        return False, "setup conditions have not all passed on prior candle"
    if rejection_candle is None or evaluation.middle is None:
        return False, "waiting for the rejection candle to close"
    if evaluation.direction == "SELL":
        matched = (
            rejection_candle.high >= evaluation.middle
            and rejection_candle.close < evaluation.middle
        )
        detail = (
            f"high={_fmt(rejection_candle.high)} ≥ mid={_fmt(evaluation.middle)} "
            f"close={_fmt(rejection_candle.close)} < mid"
        )
    else:
        matched = (
            rejection_candle.low <= evaluation.middle
            and rejection_candle.close > evaluation.middle
        )
        detail = (
            f"low={_fmt(rejection_candle.low)} ≤ mid={_fmt(evaluation.middle)} "
            f"close={_fmt(rejection_candle.close)} > mid"
        )
    return matched, detail
