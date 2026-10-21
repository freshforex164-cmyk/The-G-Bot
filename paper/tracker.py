"""In-memory price-outcome tracking for scanner signals."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class PaperOutcome:
    pair: str
    direction: str
    entry_price: float
    time: datetime
    expires_at_monotonic: float
    exit_price: float | None = None
    result: str | None = None


class PaperTracker:
    """Score later price movement for signals without monetary calculations."""

    def __init__(
        self,
        *,
        expiry_seconds: int = 30,
        summary_every: int = 10,
        output: Callable[[str], None] = print,
    ) -> None:
        if expiry_seconds <= 0:
            raise ValueError("expiry_seconds must be greater than zero")
        if summary_every <= 0:
            raise ValueError("summary_every must be greater than zero")
        self.expiry_seconds = expiry_seconds
        self.summary_every = summary_every
        self.output = output
        self.signals: list[PaperOutcome] = []
        self._open_by_pair: dict[str, list[PaperOutcome]] = defaultdict(list)
        self.wins = 0
        self.losses = 0

    @property
    def total(self) -> int:
        return len(self.signals)

    @property
    def settled(self) -> int:
        return self.wins + self.losses

    @property
    def pending(self) -> int:
        return self.total - self.settled

    @property
    def win_rate(self) -> float:
        return self.wins / self.settled * 100.0 if self.settled else 0.0

    def record_signal(
        self,
        pair: str,
        direction: str,
        entry_price: float,
        signal_time: datetime,
        *,
        monotonic_now: float | None = None,
    ) -> PaperOutcome:
        direction = direction.upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be 'BUY' or 'SELL'")
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("entry_price must be a finite number greater than zero")
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)
        now = time.monotonic() if monotonic_now is None else monotonic_now
        signal = PaperOutcome(
            pair=pair,
            direction=direction,
            entry_price=float(entry_price),
            time=signal_time,
            expires_at_monotonic=now + self.expiry_seconds,
        )
        self.signals.append(signal)
        self._open_by_pair[pair].append(signal)
        if self.total % self.summary_every == 0:
            self.print_summary()
        return signal

    def update_price(
        self,
        pair: str,
        live_price: float,
        *,
        monotonic_now: float | None = None,
    ) -> list[PaperOutcome]:
        """Settle elapsed signals using the latest price; equal prices lose."""
        if not math.isfinite(live_price):
            return []
        now = time.monotonic() if monotonic_now is None else monotonic_now
        open_signals = self._open_by_pair.get(pair, [])
        remaining: list[PaperOutcome] = []
        settled: list[PaperOutcome] = []
        for signal in open_signals:
            if now < signal.expires_at_monotonic:
                remaining.append(signal)
                continue
            signal.exit_price = float(live_price)
            won = (
                live_price < signal.entry_price
                if signal.direction == "SELL"
                else live_price > signal.entry_price
            )
            signal.result = "WIN" if won else "LOSS"
            self.wins += int(won)
            self.losses += int(not won)
            settled.append(signal)
        if remaining:
            self._open_by_pair[pair] = remaining
        else:
            self._open_by_pair.pop(pair, None)
        return settled

    def summary(self) -> str:
        return (
            f"[PAPER] signals={self.total} settled={self.settled} "
            f"wins={self.wins} losses={self.losses} pending={self.pending} "
            f"win_rate={self.win_rate:.1f}%"
        )

    def print_summary(self) -> None:
        self.output(self.summary())
