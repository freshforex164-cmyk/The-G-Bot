"""Append-only signal outcomes using live ticks with a one-candle proxy fallback."""

from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.candles import Candle
from strategy.model import Signal

SIGNAL_FIELDS = (
    "time",
    "asset",
    "setup",
    "direction",
    "entry_price",
    "expiry_price",
    "result",
    "outcome_type",
)


@dataclass
class PendingOutcome:
    signal: Signal
    due_at: datetime


class SignalOutcomeTracker:
    def __init__(self, path: str | Path = "data/signals.csv") -> None:
        self.path = Path(path)
        self.pending: dict[str, list[PendingOutcome]] = {}
        self.completed: list[dict[str, str]] = []
        if not self.path.exists() or not self.path.stat().st_size:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=SIGNAL_FIELDS).writeheader()
        else:
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                self.completed = [
                    row
                    for row in csv.DictReader(handle)
                    if row.get("result") in {"WIN", "LOSS"}
                ]

    def record_signal(self, signal: Signal) -> None:
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        pending = PendingOutcome(
            signal=signal,
            due_at=signal_time + timedelta(seconds=signal.expiry_seconds),
        )
        self.pending.setdefault(signal.pair, []).append(pending)

    def on_live_tick(
        self, pair: str, price: float, timestamp: datetime
    ) -> None:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        remaining: list[PendingOutcome] = []
        for pending in self.pending.get(pair, []):
            if timestamp < pending.due_at:
                remaining.append(pending)
            else:
                self._settle(pending, price, timestamp, "LIVE")
        self.pending[pair] = remaining

    def on_closed_candle(self, pair: str, candle: Candle, timeframe_seconds: int) -> None:
        closed_at = datetime.fromtimestamp(
            candle.time + timeframe_seconds, timezone.utc
        )
        remaining: list[PendingOutcome] = []
        for pending in self.pending.get(pair, []):
            if closed_at < pending.due_at:
                remaining.append(pending)
            else:
                self._settle(pending, candle.close, closed_at, "PROXY")
        self.pending[pair] = remaining

    def _settle(
        self,
        pending: PendingOutcome,
        expiry_price: float,
        settled_at: datetime,
        outcome_type: str,
    ) -> None:
        signal = pending.signal
        signal_time = signal.time
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        won = (
            expiry_price < signal.price
            if signal.direction == "SELL"
            else expiry_price > signal.price
        )
        row = {
            "time": signal_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "asset": signal.asset_name,
            "setup": signal.setup,
            "direction": signal.direction,
            "entry_price": f"{signal.price:.12g}",
            "expiry_price": f"{float(expiry_price):.12g}",
            "result": "WIN" if won else "LOSS",
            "outcome_type": outcome_type,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=SIGNAL_FIELDS).writerow(row)
        self.completed.append(row)

    @staticmethod
    def wilson_interval(wins: int, count: int) -> tuple[float, float] | None:
        if count <= 0:
            return None
        z = 1.96
        proportion = wins / count
        denominator = 1 + z * z / count
        center = (proportion + z * z / (2 * count)) / denominator
        margin = z * math.sqrt(
            proportion * (1 - proportion) / count
            + z * z / (4 * count * count)
        ) / denominator
        return (
            max(0.0, center - margin) * 100,
            min(1.0, center + margin) * 100,
        )

    def summary_lines(self) -> list[str]:
        grouped: dict[str, Counter[str]] = {}
        for row in self.completed:
            result = row.get("result", "")
            if result not in {"WIN", "LOSS"}:
                continue
            grouped.setdefault(row.get("setup", "UNKNOWN"), Counter())[result] += 1
        lines: list[str] = []
        for setup in sorted(grouped):
            counts = grouped[setup]
            total = counts["WIN"] + counts["LOSS"]
            interval = self.wilson_interval(counts["WIN"], total)
            low, high = interval or (0.0, 0.0)
            lines.append(
                f"OUTCOMES setup={setup} count={total} "
                f"win_rate={counts['WIN'] / total * 100:.1f}% "
                f"95%_Wilson_CI={low:.1f}–{high:.1f}%"
            )
        if not lines:
            lines.append("OUTCOMES no resolved signals")
        lines.append("BREAK-EVEN at 92% payout: 52.1%")
        return lines

    def print_summary(self) -> None:
        for line in self.summary_lines():
            print(line, flush=True)
