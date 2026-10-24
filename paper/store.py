"""Signal storage backends for in-memory and CSV operation."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import config
from strategy.pullback_reversal import Signal


@dataclass
class ExpiryOutcome:
    expiry_seconds: int
    entry_price: float | None = None
    exit_price: float | None = None
    result: str | None = None
    settled_at: datetime | None = None


@dataclass
class SignalRecord:
    signal: Signal
    entry_price: float | None = None
    entry_time: datetime | None = None
    outcomes: dict[int, ExpiryOutcome] = field(default_factory=dict)
    saved_status: str | None = None


class SignalStore(Protocol):
    def save(self, record: SignalRecord) -> None: ...


class InMemorySignalStore:
    def __init__(self) -> None:
        self.records: dict[str, SignalRecord] = {}

    def save(self, record: SignalRecord) -> None:
        self.records[record.signal.id] = record


CSV_FIELDS = (
    "id",
    "pair",
    "direction",
    "entry_price",
    "time",
    "payout",
    "strength",
    "strength_components",
    "recommended_expiry_seconds",
    "valid_until",
    "status",
    "stochastic_k",
    "stochastic_d",
    "ma_slope",
    "bollinger_middle",
    "human_delay_entry_price",
    "human_delay_entry_time",
    *(
        f"outcome_{expiry}s_{field}"
        for expiry in config.PAPER_EXPIRIES_SECONDS
        for field in ("entry_price", "exit_price", "result", "settled_at")
    ),
)


class CsvSignalStore:
    def __init__(self, path: str | Path = "signals.csv") -> None:
        self.path = Path(path)
        self._rows: dict[str, dict[str, str]] = {}
        if self.path.exists():
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    signal_id = row.get("id")
                    if signal_id:
                        self._rows[signal_id] = {
                            key: value or "" for key, value in row.items()
                        }

    def save(self, record: SignalRecord) -> None:
        signal = record.signal
        row = {field: "" for field in CSV_FIELDS}
        row.update(
            {
                "id": signal.id,
                "pair": signal.pair,
                "direction": signal.direction,
                "entry_price": f"{signal.entry_price:.12g}",
                "time": signal.time.isoformat(),
                "payout": f"{signal.payout:g}",
                "strength": str(signal.strength),
                "strength_components": json.dumps(
                    signal.strength_components, sort_keys=True
                ),
                "recommended_expiry_seconds": str(
                    signal.recommended_expiry_seconds
                ),
                "valid_until": (
                    signal.valid_until.isoformat() if signal.valid_until else ""
                ),
                "status": signal.status,
                "stochastic_k": _optional_number(signal.stochastic_k),
                "stochastic_d": _optional_number(signal.stochastic_d),
                "ma_slope": _optional_number(signal.ma_slope),
                "bollinger_middle": f"{signal.bollinger_middle:.12g}",
                "human_delay_entry_price": _optional_number(record.entry_price),
                "human_delay_entry_time": (
                    record.entry_time.isoformat() if record.entry_time else ""
                ),
            }
        )
        for expiry, outcome in record.outcomes.items():
            row[f"outcome_{expiry}s_entry_price"] = _optional_number(
                outcome.entry_price
            )
            row[f"outcome_{expiry}s_exit_price"] = _optional_number(
                outcome.exit_price
            )
            row[f"outcome_{expiry}s_result"] = outcome.result or ""
            row[f"outcome_{expiry}s_settled_at"] = (
                outcome.settled_at.isoformat() if outcome.settled_at else ""
            )
        previous = self._rows.get(signal.id, {})
        previous.update(row)
        self._rows[signal.id] = previous
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(self._rows.values())


def _optional_number(value: float | None) -> str:
    return "" if value is None else f"{value:.12g}"
