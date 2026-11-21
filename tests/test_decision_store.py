import csv
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from data.candles import Candle
from data.decision_store import DecisionStore
from strategy.model import Signal

class DecisionStoreTests(unittest.TestCase):
    def test_one_minute_outcome_resolves_and_ties_are_losses(self):
        with TemporaryDirectory() as temp:
            store = DecisionStore(Path(temp)/"decisions.csv")
            c = Candle(60, 100, 101, 99, 100)
            s = Signal("PAIR", "SELL", "RANGE_HIGH_SELL", 100, datetime.fromtimestamp(120, timezone.utc), "Pair")
            strategy = SimpleNamespace(regime_name="CONSOLIDATION")
            store.record_candle("PAIR", c, [c], strategy, s)
            self.assertEqual(store.resolved_count, 0)
            n = Candle(120, 100, 101, 99, 100)
            store.record_candle("PAIR", n, [c,n], strategy, None)
            self.assertEqual((store.signal_count, store.resolved_count, store.wins), (1,1,0))

    def test_existing_header_and_rows_are_preserved(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/"decisions.csv"
            path.write_text("event,time,custom\nOLD,earlier,keep\n")
            store = DecisionStore(path)
            c = Candle(60,1,1,1,1)
            store.record_candle("PAIR", c, [c], SimpleNamespace(), None)
            self.assertTrue(path.read_text().startswith("event,time,custom\nOLD,earlier,keep\n"))
