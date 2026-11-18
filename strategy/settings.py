"""Validated settings loaded from the editable strategy.json file."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StrategySettings:
    timeframe_seconds: int
    expiry_seconds: int
    min_regime_candles: int
    swing_lag: int
    regime_lookback: int
    min_range_atr_mult: float
    consolidation_zone_width_pct: float
    pullback_window_candles: int
    atr_period: int
    signal_valid_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "timeframe_seconds",
            "expiry_seconds",
            "min_regime_candles",
            "swing_lag",
            "regime_lookback",
            "pullback_window_candles",
            "atr_period",
            "signal_valid_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive number.")
        if self.timeframe_seconds != 60 or self.expiry_seconds != 60:
            raise ValueError("The price-action strategy requires 1-minute candles and expiry.")
        if self.regime_lookback <= self.swing_lag * 2:
            raise ValueError("regime_lookback must exceed twice swing_lag.")
        if not math.isfinite(self.min_range_atr_mult) or self.min_range_atr_mult <= 0:
            raise ValueError("min_range_atr_mult must be positive and finite.")
        if (
            not math.isfinite(self.consolidation_zone_width_pct)
            or not 0 < self.consolidation_zone_width_pct <= 0.5
        ):
            raise ValueError(
                "consolidation_zone_width_pct must be greater than 0 and at most 0.5."
            )


def load_strategy_settings(
    path: str | Path | None = None,
) -> StrategySettings:
    source = Path(path) if path is not None else Path(__file__).parents[1] / "strategy.json"
    raw: dict[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    expected = set(StrategySettings.__dataclass_fields__)
    if set(raw) != expected:
        missing = ", ".join(sorted(expected - set(raw))) or "none"
        extra = ", ".join(sorted(set(raw) - expected)) or "none"
        raise ValueError(
            f"strategy.json keys are invalid (missing: {missing}; extra: {extra})."
        )
    return StrategySettings(**raw)


STRATEGY_SETTINGS = load_strategy_settings()
