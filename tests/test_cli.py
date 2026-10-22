from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import main
from strategy.pullback_reversal import Signal


class CliTests(unittest.TestCase):
    def test_signal_line_includes_requested_cli_fields(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        signal = Signal(
            pair="BITB_otc",
            direction="SELL",
            price=24.0017,
            time=now,
            stochastic_k=80,
            stochastic_d=75,
            ma_slope=-0.01,
            bollinger_middle=24.0,
            payout=92,
            strength=84,
            valid_until=now + timedelta(seconds=15),
        )
        self.assertEqual(
            main._format_signal_line(signal),
            "2026-01-01T00:00:00+00:00 BITB_otc SELL payout=92% "
            "strength=84 expiry=60s valid-for=15s",
        )

    def test_scanner_command_passes_minutes(self) -> None:
        with patch.object(main, "run_scanner", new_callable=AsyncMock) as scanner:
            self.assertEqual(main.main(["--run", "scanner", "--minutes", "0.5"]), 0)
        scanner.assert_awaited_once_with(minutes=0.5, record=False)

    def test_scanner_record_flag_is_forwarded(self) -> None:
        with patch.object(main, "run_scanner", new_callable=AsyncMock) as scanner:
            self.assertEqual(
                main.main(["--run", "scanner", "--minutes", "1", "--record"]), 0
            )
        scanner.assert_awaited_once_with(minutes=1.0, record=True)

    def test_notify_test_command_returns_failure_code_when_send_fails(self) -> None:
        with patch.object(
            main, "run_notify_test", new_callable=AsyncMock, return_value=False
        ):
            self.assertEqual(main.main(["--run", "notify-test"]), 1)


if __name__ == "__main__":
    unittest.main()
