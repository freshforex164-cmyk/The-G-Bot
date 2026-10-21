"""Confirmation-quality signal strength scoring."""

from __future__ import annotations

import config


def _clamp_ratio(value: float, reference: float) -> float:
    if reference <= 0:
        raise ValueError("strength references must be greater than zero")
    return min(1.0, max(0.0, value / reference))


def score_strength(
    *,
    stochastic_extremity: float,
    stochastic_turn: float,
    moving_average_turn: float,
    bollinger_slope: float,
    bollinger_curvature: float,
    touch_cleanliness: float,
) -> tuple[int, dict[str, int]]:
    """Return 0–100 strength and weighted component contributions.

    Market payout is deliberately not accepted here, so it cannot influence the
    score.
    """
    weights = config.STRENGTH_WEIGHTS
    if set(weights) != {
        "stochastic",
        "moving_average",
        "bollinger",
        "middle_band_touch",
    } or sum(weights.values()) != 100:
        raise ValueError("strength component weights must be the four components and sum to 100")

    normalized = {
        "stochastic": (
            _clamp_ratio(
                stochastic_extremity,
                config.STRENGTH_STOCHASTIC_EXTREMITY_REFERENCE,
            )
            + _clamp_ratio(
                stochastic_turn, config.STRENGTH_STOCHASTIC_TURN_REFERENCE
            )
        )
        / 2,
        "moving_average": _clamp_ratio(
            moving_average_turn,
            config.STRENGTH_MA_TURN_REFERENCE_PERCENT,
        ),
        "bollinger": (
            _clamp_ratio(
                bollinger_slope,
                config.STRENGTH_BB_SLOPE_REFERENCE_PERCENT,
            )
            + _clamp_ratio(
                bollinger_curvature,
                config.STRENGTH_BB_CURVATURE_REFERENCE_PERCENT,
            )
        )
        / 2,
        "middle_band_touch": _clamp_ratio(touch_cleanliness, 1.0),
    }
    components = {
        name: round(normalized[name] * weight)
        for name, weight in weights.items()
    }
    return min(100, sum(components.values())), components
