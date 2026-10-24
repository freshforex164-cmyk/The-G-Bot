from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from data.candles import Candle
from data.decision_store import DecisionStore
from strategy.pullback_reversal import Signal


class DecisionStoreTests(unittest.TestCase):
    def test_logs_each_direction_and_delayed_outcomes_with_ties_as_losses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "decisions.csv"
            store = DecisionStore(path)
            strategy = SimpleNamespace(
                states={
                    ("PAIR_otc", "SELL"): SimpleNamespace(
                        status="FIRE", reason="MIDDLE"
                    ),
                    ("PAIR_otc", "BUY"): SimpleNamespace(
                        status="CANCEL", reason="opposite direction fired"
                    ),
                }
            )
            candles = [
                Candle(1000 + index * 15, 100, 101, 99, 100)
                for index in range(40)
            ]
            signal = Signal(
                pair="PAIR_otc",
                direction="SELL",
                price=100,
                time=datetime.fromtimestamp(candles[-1].time, timezone.utc),
                stochastic_k=75,
                stochastic_d=70,
                ma_slope=-0.01,
                bollinger_middle=100,
                strength=82,
            )

            store.record_candle("PAIR_otc", candles[-1], candles, strategy, signal)
            for offset, close in ((1, 98), (2, 100), (3, 99)):
                future = Candle(
                    candles[-1].time + offset * 15, close, close, close, close
                )
                history = candles + [
                    Candle(
                        candles[-1].time + index * 15,
                        price,
                        price,
                        price,
                        price,
                    )
                    for index, price in ((1, 98), (2, 100), (3, 99))[:offset]
                ]
                store.record_candle("PAIR_otc", future, history, strategy, None)

            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        decision_rows = [row for row in rows if row["event"] == "DECISION"]
        outcome_rows = [row for row in rows if row["event"] == "OUTCOME"]
        self.assertEqual(len(decision_rows), 8)
        self.assertEqual(
            {(row["direction"], row["state"]) for row in decision_rows[:2]},
            {("SELL", "FIRE"), ("BUY", "CANCEL")},
        )
        self.assertEqual(decision_rows[1]["reason"], "opposite direction fired")
        self.assertEqual(len(outcome_rows), 1)
        self.assertEqual(outcome_rows[0]["signal_strength"], "82")
        self.assertEqual(outcome_rows[0]["candle_2_close"], "100")
        self.assertEqual(outcome_rows[0]["candle_3_close"], "99")
        self.assertEqual(outcome_rows[0]["outcome_35s_proxy"], "LOSS")
        self.assertIn("break_even_at_92_percent_payout=52.1%", store.summary())
        self.assertIn("95%_Wilson_CI=0.0–79.3%", store.summary())


if __name__ == "__main__":
    unittest.main()
