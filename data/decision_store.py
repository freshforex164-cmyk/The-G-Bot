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
from strategy.model import Signal

DECISION_FIELDS = (
    "event", "time", "pair", "direction", "state", "reason", "atr",
    "signal_id", "entry_price", "expiry_close", "outcome_1m_proxy",
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
    """Append every closed-candle decision and delayed outcome proxy."""

    def __init__(self, file_path: str | Path = "data/decisions.csv") -> None:
        self.file_path = Path(file_path)
        self.signal_count = 0
        self.resolved_count = 0
        self.wins = 0
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._ensure_header()

    def _ensure_header(self) -> None:
        # Existing append-only files retain their original header and all historical rows.
        if self.file_path.exists() and self.file_path.stat().st_size:
            with self.file_path.open(newline="", encoding="utf-8") as handle:
                self._fields = tuple(next(csv.reader(handle)))
            return
        self._fields = DECISION_FIELDS
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.file_path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self._fields).writeheader()

    def _append(self, row: dict[str, Any]) -> None:
        with self.file_path.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=self._fields).writerow(
                {key: row.get(key, "") for key in self._fields})

    def record_candle(
        self,
        pair: str,
        candle: Candle,
        candles: Sequence[Candle],
        strategy: Any,
        signal: Signal | None,
    ) -> None:
        """Record closed-minute decisions and a one-minute candle-close outcome proxy."""
        if candle.time % 60:
            raise ValueError("Decision candles must start on whole minutes.")
        for direction in ("SELL", "BUY"):
            selected = signal if signal is not None and signal.direction == direction else None
            self._append({
                "event": "DECISION",
                "time": datetime.fromtimestamp(candle.time, timezone.utc).isoformat(),
                "pair": pair, "direction": direction,
                "state": getattr(strategy, "regime_name", "CONSOLIDATION"),
                "reason": selected.setup if selected else "no qualifying closed-candle setup",
                "atr": _number(calculate_atr(candles)[-1]) if candles else "",
                "signal_id": selected.id if selected else "",
                "entry_price": _number(selected.price) if selected else "",
            })
            if selected is not None:
                self.signal_count += 1
                self._pending.setdefault(pair, []).append({"signal": selected, "time": candle.time})
        remaining = []
        for pending in self._pending.get(pair, []):
            if candle.time < pending["time"] + 60:
                remaining.append(pending)
                continue
            selected = pending["signal"]
            # Missing minutes are not fabricated or counted as wins/losses.
            if candle.time != pending["time"] + 60:
                continue
            won = candle.close < selected.price if selected.direction == "SELL" else candle.close > selected.price
            self.resolved_count += 1
            self.wins += int(won)
            self._append({"event": "OUTCOME", "time": selected.time.isoformat(), "pair": pair,
                          "direction": selected.direction, "signal_id": selected.id,
                          "entry_price": _number(selected.price), "expiry_close": _number(candle.close),
                          "outcome_1m_proxy": "WIN" if won else "LOSS",
                          "reason": "one-minute candle-close proxy; ties are losses"})
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
