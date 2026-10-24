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
            "strength=84 expiry=35s valid-for=15s",
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

    def test_explain_options_are_forwarded(self) -> None:
        with patch.object(main, "run_explain", new_callable=AsyncMock) as explain:
            self.assertEqual(
                main.main(
                    ["--run", "explain", "--pair", "BITB_otc", "--seconds", "30"]
                ),
                0,
            )
        explain.assert_awaited_once_with(pair="BITB_otc", seconds=30)

    def test_strategy_lab_alias_runs_replay_command(self) -> None:
        with patch.object(main, "run_replay", new_callable=AsyncMock) as replay:
            self.assertEqual(main.main(["--run", "strategy-lab"]), 0)
        replay.assert_awaited_once_with()

    def test_notify_test_command_returns_failure_code_when_send_fails(self) -> None:
        with patch.object(
            main, "run_notify_test", new_callable=AsyncMock, return_value=False
        ):
            self.assertEqual(main.main(["--run", "notify-test"]), 1)

    def test_feed_check_command_returns_failure_code_when_feed_fails(self) -> None:
        with patch.object(
            main,
            "run_feed_check",
            new_callable=AsyncMock,
            return_value=main.FeedCheckResult(
                ok=False,
                tick_count=0,
                last_price=None,
                reason="no ticks received",
            ),
        ):
            self.assertEqual(main.main(["--run", "feed-check"]), 1)

    def test_pairs_command_calls_live_pair_listing(self) -> None:
        with patch.object(main, "run_pairs", new_callable=AsyncMock, return_value=True) as pairs:
            self.assertEqual(main.main(["--run", "pairs"]), 0)
        pairs.assert_awaited_once_with()

    def test_serve_command_starts_bot_and_scanner_service(self) -> None:
        with patch.object(main, "run_serve", new_callable=AsyncMock) as serve:
            self.assertEqual(main.main(["--run", "serve"]), 0)
        serve.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
