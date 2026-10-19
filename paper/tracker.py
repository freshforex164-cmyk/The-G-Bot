"""In-memory paper outcome tracking for strategy signals."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass
class PaperTrade:
    pair: str
    direction: str
    entry_price: float
    time: datetime
    payout_percent: float
    expires_at_monotonic: float
    exit_price: float | None = None
    result: str | None = None
    profit: float = 0.0


class PaperTracker:
    """Track signal outcomes against later live prices without placing orders."""

    def __init__(
        self,
        *,
        expiry_seconds: int = config.EXPIRY_SECONDS,
        stake: float = config.PAPER_STAKE,
        summary_every: int = config.PAPER_SUMMARY_EVERY_SIGNALS,
        output: Callable[[str], None] = print,
    ) -> None:
        if expiry_seconds <= 0:
            raise ValueError("expiry_seconds must be greater than zero")
        if stake <= 0 or not math.isfinite(stake):
            raise ValueError("stake must be a finite number greater than zero")
        if summary_every <= 0:
            raise ValueError("summary_every must be greater than zero")
        self.expiry_seconds = expiry_seconds
        self.stake = stake
        self.summary_every = summary_every
        self.output = output
        self.trades: list[PaperTrade] = []
        self._open_by_pair: dict[str, list[PaperTrade]] = defaultdict(list)
        self.wins = 0
        self.losses = 0
        self.estimated_profit = 0.0

    @property
    def total(self) -> int:
        """Number of signals recorded, including any not yet expired."""
        return len(self.trades)

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
        payout_percent: float,
        *,
        monotonic_now: float | None = None,
    ) -> PaperTrade:
        direction = direction.upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be 'BUY' or 'SELL'")
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("entry_price must be a finite number greater than zero")
        if not math.isfinite(payout_percent) or payout_percent < 0:
            raise ValueError("payout_percent must be a finite non-negative number")
        if signal_time.tzinfo is None:
            signal_time = signal_time.replace(tzinfo=timezone.utc)

        now = time.monotonic() if monotonic_now is None else monotonic_now
        trade = PaperTrade(
            pair=pair,
            direction=direction,
            entry_price=float(entry_price),
            time=signal_time,
            payout_percent=float(payout_percent),
            expires_at_monotonic=now + self.expiry_seconds,
        )
        self.trades.append(trade)
        self._open_by_pair[pair].append(trade)
        if self.total % self.summary_every == 0:
            self.print_summary()
        return trade

    def update_price(
        self,
        pair: str,
        live_price: float,
        *,
        monotonic_now: float | None = None,
    ) -> list[PaperTrade]:
        """Settle expired signals for this pair using the current live price."""
        if not math.isfinite(live_price):
            return []
        now = time.monotonic() if monotonic_now is None else monotonic_now
        open_trades = self._open_by_pair.get(pair, [])
        remaining: list[PaperTrade] = []
        settled_now: list[PaperTrade] = []

        for trade in open_trades:
            if now < trade.expires_at_monotonic:
                remaining.append(trade)
                continue
            trade.exit_price = float(live_price)
            won = (
                trade.exit_price < trade.entry_price
                if trade.direction == "SELL"
                else trade.exit_price > trade.entry_price
            )
            if won:
                trade.result = "WIN"
                trade.profit = self.stake * trade.payout_percent / 100.0
                self.wins += 1
            else:
                # An equal exit price is intentionally a loss.
                trade.result = "LOSS"
                trade.profit = -self.stake
                self.losses += 1
            self.estimated_profit += trade.profit
            settled_now.append(trade)

        if remaining:
            self._open_by_pair[pair] = remaining
        else:
            self._open_by_pair.pop(pair, None)
        return settled_now

    def summary(self) -> str:
        return (
            f"[PAPER] signals={self.total} settled={self.settled} "
            f"wins={self.wins} losses={self.losses} pending={self.pending} "
            f"win_rate={self.win_rate:.1f}% "
            f"estimated_profit=${self.estimated_profit:.2f}"
        )

    def print_summary(self) -> None:
        self.output(self.summary())
