"""Delayed-entry, multi-expiry outcome tracking without monetary calculations."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone

import config
from paper.store import (
    ExpiryOutcome,
    InMemorySignalStore,
    SignalRecord,
    SignalStore,
)
from strategy.pullback_reversal import Signal


class PaperTracker:
    def __init__(
        self,
        *,
        store: SignalStore | None = None,
        expiries_seconds: Iterable[int] = config.PAPER_EXPIRIES_SECONDS,
        human_delay_seconds: int = config.HUMAN_DELAY_SECONDS,
        output: Callable[[str], None] = print,
    ) -> None:
        expiries = tuple(sorted(set(expiries_seconds)))
        if expiries != (60,):
            raise ValueError("Only 1-minute paper expiry is supported.")
        if human_delay_seconds < 0:
            raise ValueError("human delay cannot be negative")
        self.expiries_seconds = expiries
        self.human_delay_seconds = human_delay_seconds
        self.store = store or InMemorySignalStore()
        self.output = output
        self.records: dict[str, SignalRecord] = {}

    def record_signal(self, signal: Signal) -> SignalRecord:
        if signal.direction not in {"BUY", "SELL"}:
            raise ValueError("signal direction must be BUY or SELL")
        if not math.isfinite(signal.entry_price) or signal.entry_price <= 0:
            raise ValueError("signal entry price must be positive and finite")
        record = SignalRecord(
            signal=signal,
            outcomes={
                expiry: ExpiryOutcome(expiry_seconds=expiry)
                for expiry in self.expiries_seconds
            },
        )
        self.records[signal.id] = record
        self._persist(record)
        return record

    def update_price(
        self,
        pair: str,
        live_price: float,
        timestamp: datetime,
    ) -> list[ExpiryOutcome]:
        if not math.isfinite(live_price):
            return []
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        settled: list[ExpiryOutcome] = []
        for record in self.records.values():
            signal = record.signal
            if signal.pair != pair:
                continue
            if signal.time.tzinfo is None:
                signal.time = signal.time.replace(tzinfo=timezone.utc)
            changed = False
            entry_at = signal.time + timedelta(seconds=self.human_delay_seconds)
            if record.entry_price is None and timestamp >= entry_at:
                record.entry_price = float(live_price)
                record.entry_time = timestamp
                for outcome in record.outcomes.values():
                    outcome.entry_price = float(live_price)
                changed = True

            if record.entry_price is not None and record.entry_time is not None:
                for expiry, outcome in record.outcomes.items():
                    due_at = record.entry_time + timedelta(seconds=expiry)
                    if outcome.result is not None or timestamp < due_at:
                        continue
                    outcome.exit_price = float(live_price)
                    outcome.settled_at = timestamp
                    won = (
                        live_price < record.entry_price
                        if signal.direction == "SELL"
                        else live_price > record.entry_price
                    )
                    outcome.result = "WIN" if won else "LOSS"
                    settled.append(outcome)
                    changed = True

            if changed or record.saved_status != signal.status:
                self._persist(record)
        return settled

    def _persist(self, record: SignalRecord) -> None:
        self.store.save(record)
        record.saved_status = record.signal.status

    @staticmethod
    def _win_rate(wins: int, count: int) -> float:
        return wins / count * 100.0 if count else 0.0

    def summary_lines(self) -> list[str]:
        for record in self.records.values():
            if record.saved_status != record.signal.status:
                self._persist(record)

        lines: list[str] = []
        for expiry in self.expiries_seconds:
            outcomes = [
                record.outcomes[expiry]
                for record in self.records.values()
                if record.outcomes[expiry].result is not None
            ]
            wins = sum(outcome.result == "WIN" for outcome in outcomes)
            count = len(outcomes)
            lines.append(
                f"PAPER expiry={expiry}s count={count} wins={wins} "
                f"win_rate={self._win_rate(wins, count):.1f}%"
            )

        return lines

    def summary(self) -> str:
        return "\n".join(self.summary_lines())

    def print_summary(self) -> None:
        for line in self.summary_lines():
            self.output(line)
