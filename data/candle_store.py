"""Append and reload recorded closed candles for approximate replay."""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Iterable

from data.candles import Candle

FIELDS = ("pair", "time", "open", "high", "low", "close")


class CsvCandleStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, pair: str, candle: Candle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        with self.path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow(
                {
                    "pair": pair,
                    "time": candle.time,
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                }
            )

    def load(self) -> dict[str, list[Candle]]:
        if not self.path.exists():
            return {}
        candles: dict[str, list[Candle]] = defaultdict(list)
        with self.path.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                pair = row.get("pair", "").strip()
                if not pair:
                    continue
                candles[pair].append(
                    Candle(
                        time=int(row["time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                    )
                )
        return {
            pair: sorted(series, key=lambda candle: candle.time)
            for pair, series in candles.items()
        }
