"""Append-only candle decisions and approximate signal outcome measurements."""

from __future__ import annotations

import csv
import math
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from data.candles import Candle
from indicators.atr import calculate_atr
from indicators.bollinger_bands import calculate_bollinger_bands
from indicators.moving_average import calculate_moving_average
from indicators.stochastic import calculate_stochastic
from strategy.pullback_reversal import Signal

DECISION_FIELDS = (
    "event",
    "time",
    "pair",
    "direction",
    "state",
    "reason",
    "stochastic_k",
    "stochastic_d",
    "sma",
    "bollinger_middle",
    "bollinger_upper",
    "bollinger_lower",
    "atr",
    "signal_id",
    "signal_strength",
    "candle_2_close",
    "candle_3_close",
    "outcome_35s_proxy",
)


def _number(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{number:.12g}" if math.isfinite(number) else ""


def _wilson_interval(wins: int, count: int) -> tuple[float, float] | None:
    if count <= 0:
        return None
    z = 1.96
    proportion = wins / count
    denominator = 1 + z * z / count
    center = (proportion + z * z / (2 * count)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / count
            + z * z / (4 * count * count)
        )
        / denominator
    )
    return (max(0.0, center - margin) * 100, min(1.0, center + margin) * 100)


class DecisionStore:
    """Append every closed-candle decision and delayed two/three-candle outcomes."""

    def __init__(self, file_path: str | Path = "data/decisions.csv") -> None:
        self.file_path = Path(file_path)
        self.signal_count = 0
        self.resolved_count = 0
        self.wins = 0
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._ensure_header()

    def _ensure_header(self) -> None:
        if self.file_path.exists() and self.file_path.stat().st_size:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=DECISION_FIELDS).writeheader()

    def _append(self, row: dict[str, Any]) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDS)
            writer.writerow({key: row.get(key, "") for key in DECISION_FIELDS})

    @staticmethod
    def _indicators(candles: Sequence[Candle]) -> dict[str, str]:
        values = {
            "stochastic_k": "",
            "stochastic_d": "",
            "sma": "",
            "bollinger_middle": "",
            "bollinger_upper": "",
            "bollinger_lower": "",
            "atr": "",
        }
        if not candles:
            return values
        index = len(candles) - 1
        try:
            stochastic = calculate_stochastic(candles)
            bands = calculate_bollinger_bands(candles)
            moving_average = calculate_moving_average(
                candles,
                period=config.MOVING_AVERAGE_PERIOD,
                method=config.MOVING_AVERAGE_METHOD,
            )
            atr = calculate_atr(candles)
            values.update(
                stochastic_k=_number(stochastic.slow_k[index]),
                stochastic_d=_number(stochastic.slow_d[index]),
                sma=_number(moving_average[index]),
                bollinger_middle=_number(bands.middle[index]),
                bollinger_upper=_number(bands.upper[index]),
                bollinger_lower=_number(bands.lower[index]),
                atr=_number(atr[index]),
            )
        except (IndexError, TypeError, ValueError, ZeroDivisionError):
            pass
        return values

    def record_candle(
        self,
        pair: str,
        candle: Candle,
        candles: Sequence[Candle],
        strategy: Any,
        signal: Signal | None,
    ) -> None:
        """Record both directional states, then resolve signals as closes arrive."""
        timestamp = candle.time
        time_text = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
        indicators = self._indicators(candles)
        states = getattr(strategy, "states", {})
        state_getter = getattr(strategy, "state", None)
        for direction in ("SELL", "BUY"):
            direction_state = (
                state_getter(pair, direction)
                if callable(state_getter)
                else states.get((pair, direction))
            )
            status = getattr(direction_state, "status", "IDLE")
            reason = getattr(direction_state, "reason", "")
            if not reason:
                reason = (
                    "indicator history unavailable"
                    if len(candles) < config.MIN_STRATEGY_CANDLES
                    else "no qualifying setup"
                )
            selected = signal if signal is not None and signal.direction == direction else None
            row = {
                "event": "DECISION",
                "time": time_text,
                "pair": pair,
                "direction": direction,
                "state": status,
                "reason": reason,
                **indicators,
                "signal_id": selected.id if selected else "",
                "signal_strength": selected.strength if selected else "",
            }
            self._append(row)
            if selected is not None:
                self.signal_count += 1
                self._pending.setdefault(pair, []).append(
                    {
                        "index": len(candles) - 1,
                        "time": time_text,
                        "signal": selected,
                        "candle_2_close": None,
                    }
                )

        current_index = len(candles) - 1
        remaining: list[dict[str, Any]] = []
        for pending in self._pending.get(pair, []):
            signal_index = int(pending["index"])
            candle_2_index = signal_index + 2
            candle_3_index = signal_index + 3
            if current_index >= candle_2_index and pending["candle_2_close"] is None:
                pending["candle_2_close"] = float(candles[candle_2_index].close)
            if current_index < candle_3_index:
                remaining.append(pending)
                continue
            signal = pending["signal"]
            candle_2_close = pending["candle_2_close"]
            candle_3_close = float(candles[candle_3_index].close)
            if candle_2_close is None:
                remaining.append(pending)
                continue
            is_win = (
                candle_2_close < signal.price
                if signal.direction == "SELL"
                else candle_2_close > signal.price
            )
            outcome = "WIN" if is_win else "LOSS"
            self.resolved_count += 1
            self.wins += int(is_win)
            self._append(
                {
                    "event": "OUTCOME",
                    "time": pending["time"],
                    "pair": pair,
                    "direction": signal.direction,
                    "state": "FIRE",
                    "reason": "35s outcome uses the candle+2 close (30s proxy); ties are losses",
                    "signal_id": signal.id,
                    "signal_strength": signal.strength,
                    "candle_2_close": _number(candle_2_close),
                    "candle_3_close": _number(candle_3_close),
                    "outcome_35s_proxy": outcome,
                }
            )
        self._pending[pair] = remaining

    def summary(self) -> str:
        break_even = 100 / (1 + config.BREAKEVEN_PAYOUT_PERCENT / 100)
        if not self.resolved_count:
            rate_text, interval_text = "n/a", "n/a"
        else:
            rate = self.wins / self.resolved_count * 100
            interval = _wilson_interval(self.wins, self.resolved_count)
            assert interval is not None
            rate_text = f"{rate:.1f}%"
            interval_text = f"{interval[0]:.1f}–{interval[1]:.1f}%"
        return (
            f"DECISIONS signals={self.signal_count} resolved={self.resolved_count} "
            f"win_rate={rate_text} break_even_at_92_percent_payout={break_even:.1f}% "
            f"95%_Wilson_CI={interval_text}"
        )

    def print_summary(self) -> None:
        print(self.summary(), flush=True)
