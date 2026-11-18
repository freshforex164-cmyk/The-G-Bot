from __future__ import annotations

import io
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class StrategyDebugCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_reads_catalogue_history_without_printing_credentials(self) -> None:
        class FakeBroker:
            closed = False

            def __init__(self, ssid: str) -> None:
                self.ssid = ssid

            async def connect(self) -> None:
                return None

            async def active_assets(self):
                return [
                    {
                        "symbol": "AEDCNY_otc",
                        "name": "AED/CNY",
                        "is_active": True,
                    }
                ]

            async def candle_history(self, symbol, period, lookback):
                self.request = (symbol, period, lookback)
                bucket = int(time.time()) // 60 * 60 - 60 * 60
                return [
                    {
                        "timestamp": bucket + index * 60 + 7200,
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                    }
                    for index in range(60)
                ]

            async def close(self) -> None:
                type(self).closed = True

        output = io.StringIO()
        with (
            patch.object(main, "PocketOptionBroker", FakeBroker),
            patch.object(main.config, "load_ssid", return_value="private-session"),
            redirect_stdout(output),
        ):
            result = await main.run_strategy_debug("aed-cny")

        self.assertTrue(result)
        self.assertTrue(FakeBroker.closed)
        text = output.getvalue()
        self.assertIn("STRATEGY DEBUG asset=AED/CNY", text)
        self.assertIn("ACTIVE LEVELS", text)
        self.assertNotIn("private-session", text)
        self.assertNotIn("ssid", text.lower())

    async def test_unknown_asset_is_rejected_before_broker_access(self) -> None:
        with (
            patch.object(main, "PocketOptionBroker") as broker,
            redirect_stdout(io.StringIO()),
        ):
            result = await main.run_strategy_debug("not-a-catalogue-id")
        self.assertFalse(result)
        broker.assert_not_called()

    async def test_broker_failure_output_uses_exception_type_only(self) -> None:
        class FakeBroker:
            def __init__(self, _ssid: str) -> None:
                pass

            async def connect(self) -> None:
                raise RuntimeError("request contained private-session")

            async def close(self) -> None:
                return None

        output = io.StringIO()
        with (
            patch.object(main, "PocketOptionBroker", FakeBroker),
            patch.object(main.config, "load_ssid", return_value="private-session"),
            redirect_stdout(output),
        ):
            result = await main.run_strategy_debug("aed-cny")
        self.assertFalse(result)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("private-session", output.getvalue())


if __name__ == "__main__":
    unittest.main()
