from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper.store import CSV_FIELDS, CsvSignalStore, InMemorySignalStore
from paper.tracker import PaperTracker
from strategy.pullback_reversal import Signal


def make_signal(at: datetime) -> Signal:
    return Signal(
        pair="EURUSD_otc",
        direction="BUY",
        price=1.2345,
        time=at,
        stochastic_k=20.0,
        stochastic_d=18.0,
        ma_slope=0.002,
        bollinger_middle=1.234,
        payout=88,
        strength=72,
        strength_components={
            "stochastic": 20,
            "moving_average": 18,
            "bollinger": 19,
            "middle_band_touch": 15,
        },
        valid_until=at + timedelta(seconds=15),
    )


class SignalStoreTests(unittest.TestCase):
    def test_default_store_keeps_signal_records_in_memory(self) -> None:
        tracker = PaperTracker(output=lambda _: None)
        record = tracker.record_signal(
            make_signal(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
        self.assertIsInstance(tracker.store, InMemorySignalStore)
        self.assertIs(tracker.store.records[record.signal.id], record)

    def test_csv_contains_signal_fields_delayed_entry_and_expiry_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "signals.csv"
            tracker = PaperTracker(
                store=CsvSignalStore(path),
                output=lambda _: None,
            )
            self.assertEqual(
                path.read_text(encoding="utf-8").splitlines()[0],
                ",".join(CSV_FIELDS),
            )
            at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            signal = make_signal(at)
            tracker.record_signal(signal)
            tracker.update_price("EURUSD_otc", 1.25, at + timedelta(seconds=5))
            tracker.update_price("EURUSD_otc", 1.26, at + timedelta(seconds=40))

            with path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["id"], signal.id)
        self.assertEqual(row["pair"], "EURUSD_otc")
        self.assertEqual(row["direction"], "BUY")
        self.assertEqual(row["payout"], "88")
        self.assertEqual(row["strength"], "72")
        self.assertEqual(json.loads(row["strength_components"])["bollinger"], 19)
        self.assertEqual(row["recommended_expiry_seconds"], "35")
        self.assertEqual(row["human_delay_entry_price"], "1.25")
        self.assertEqual(row["outcome_35s_entry_price"], "1.25")
        self.assertEqual(row["outcome_35s_exit_price"], "1.26")
        self.assertEqual(row["outcome_35s_result"], "WIN")


if __name__ == "__main__":
    unittest.main()
