"""Validated settings loaded from the editable strategy.json file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StrategySettings:
    timeframe_seconds: int
    expiry_seconds: int
    swing_side: int
    trend_swings: int
    range_candles: int
    zone_fraction: float
    range_min_height_atr: float
    atr_period: int
    signal_valid_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "timeframe_seconds",
            "expiry_seconds",
            "swing_side",
            "trend_swings",
            "range_candles",
            "atr_period",
            "signal_valid_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive number.")
        if not 0 < self.zone_fraction < 1:
            raise ValueError("zone_fraction must be between 0 and 1.")
        if self.range_min_height_atr <= 0:
            raise ValueError("range_min_height_atr must be positive.")


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
