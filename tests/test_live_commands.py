from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from data.candle_store import CsvCandleStore
from data.candles import Candle


class FakeResponse:
    status = 200

    def __init__(self, body: bytes = b'{"ok": true}') -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status



class CandleCsvTests(unittest.TestCase):
    def test_candle_store_appends_unique_closes_and_reloads_by_pair(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "candles.csv"
            store = CsvCandleStore(path)
            self.assertEqual(
                path.read_text(encoding="utf-8").splitlines()[0],
                "pair,time,open,high,low,close",
            )
            candle = Candle(100, 1.0, 1.2, 0.9, 1.1)
            store.append("PAIR", candle)
            store.append("PAIR", candle)
            loaded = CsvCandleStore(path).load()

        self.assertEqual(loaded, {"PAIR": [candle]})


if __name__ == "__main__":
    unittest.main()
