from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import main


class CliTests(unittest.TestCase):

    def test_strategy_debug_requires_and_forwards_one_catalogue_asset(self) -> None:
        with patch.object(
            main, "run_strategy_debug", new_callable=AsyncMock, return_value=True
        ) as debug:
            self.assertEqual(
                main.main(
                    ["--run", "strategy-debug", "--pair", "aed-cny"]
                ),
                0,
            )
        debug.assert_awaited_once_with("aed-cny")


    def test_notify_test_command_returns_failure_code_when_send_fails(self) -> None:
        with patch.object(
            main, "run_notify_test", new_callable=AsyncMock, return_value=False
        ):
            self.assertEqual(main.main(["--run", "notify-test"]), 1)


    def test_serve_command_starts_bot_and_scanner_service(self) -> None:
        with patch.object(main, "run_serve", new_callable=AsyncMock) as serve:
            self.assertEqual(main.main(["--run", "serve"]), 0)
        serve.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
