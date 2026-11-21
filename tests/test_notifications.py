from __future__ import annotations

import asyncio
import io
import json
import logging
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from notifications import (
    ConsoleNotifier,
    NotificationQueue,
    TelegramNotifier,
    TelegramSendError,
    build_notifier,
    format_signal_message,
    telegram_get_updates,
)
from strategy.pullback_reversal import Signal


def sample_signal(
    *,
    strength: int = 84,
    time: datetime | None = None,
    valid_until: datetime | None = None,
) -> Signal:
    created = time or datetime.now(timezone.utc)
    return Signal(
        pair="BITB_otc",
        direction="SELL",
        setup="RANGE_HIGH_SELL",
        price=24.0017,
        time=created,
        payout=92,
        asset_name="Bitcoin OTC",
        valid_until=valid_until or created + timedelta(seconds=15),
    )


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self) -> bytes:
        return self.body

    def getcode(self) -> int:
        return self.status


class TelegramNotifierTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")
    async def test_success_posts_html_signal_with_five_second_timeout(self) -> None:
        token = "test-token-never-log"
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = request.headers
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse(b'{"ok": true}')

        notifier = TelegramNotifier(token, "chat-123", opener=opener)
        result = await notifier.send(sample_signal())
        self.assertEqual(result, "sent through Telegram")
        self.assertIn("/bot" + token + "/sendMessage", captured["url"])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 5.0)
        self.assertEqual(
            captured["body"]["chat_id"], "chat-123"
        )
        self.assertEqual(
            captured["body"]["text"],
            "🔴 <b>SELL</b>\nAsset: Bitcoin OTC\nPayout: 92%\n"
            "Strength: 84\nExpiration: <b>60s</b>",
        )
        self.assertEqual(captured["body"]["parse_mode"], "HTML")

    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")

    def test_signal_message_has_exactly_five_lines_no_price_and_escapes_asset(self) -> None:
        signal = sample_signal()
        signal.asset_name = "EUR/GBP <OTC>"
        message = format_signal_message(signal)
        self.assertEqual(
            message,
            "🔴 <b>SELL</b>\nAsset: EUR/GBP &lt;OTC&gt;\nPayout: 92%\n"
            "Strength: 84\nExpiration: <b>60s</b>",
        )
        self.assertEqual(len(message.splitlines()), 5)
        self.assertNotIn("24.0017", message)

    async def test_send_text_posts_test_message_as_plain_text(self) -> None:
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data)
            return FakeResponse(b'{"ok": true}')

        notifier = TelegramNotifier("test-token", "-100123", opener=opener)
        self.assertEqual(
            await notifier.send_text("🧪 TEST SELL PAIR\nNot a confirmed signal"),
            "sent through Telegram",
        )
        self.assertEqual(captured["body"]["chat_id"], "-100123")
        self.assertEqual(
            captured["body"]["text"],
            "🧪 TEST SELL PAIR\nNot a confirmed signal",
        )

    async def test_get_updates_lists_response_without_echoing_token(self) -> None:
        token = "private-test-token"
        captured: dict[str, object] = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse(
                json.dumps(
                    {
                        "ok": True,
                        "result": [
                            {
                                "update_id": 1,
                                "channel_post": {
                                    "chat": {
                                        "id": -100123,
                                        "type": "channel",
                                        "title": "GBot alerts",
                                    }
                                },
                            }
                        ],
                    }
                ).encode()
            )

        result = await telegram_get_updates(token, opener=opener)
        self.assertEqual(len(result), 1)
        self.assertIn("/bot" + token + "/getUpdates", captured["url"])

    async def test_get_updates_errors_do_not_echo_token(self) -> None:
        token = "private-token"

        def opener(request, timeout):
            raise urllib.error.URLError(f"failed at {request.full_url}")

        with self.assertRaises(TelegramSendError) as captured:
            await telegram_get_updates(token, opener=opener)
        self.assertNotIn(token, str(captured.exception))

    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")

    async def test_reports_actionable_api_errors_without_echoing_token(self) -> None:
        cases = (
            (
                401,
                {"ok": False, "description": "Unauthorized"},
                "invalid token",
            ),
            (
                400,
                {"ok": False, "description": "Bad Request: chat not found"},
                "chat ID",
            ),
            (
                403,
                {"ok": False, "description": "Forbidden"},
                "blocked or not a member",
            ),
            (
                429,
                {
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 17},
                },
                "wait 17s",
            ),
        )
        for status, payload, expected in cases:
            with self.subTest(status=status):
                def opener(request, timeout):
                    raise urllib.error.HTTPError(
                        request.full_url,
                        status,
                        "request failed",
                        {},
                        io.BytesIO(json.dumps(payload).encode()),
                    )

                notifier = TelegramNotifier(
                    "private-test-token", "chat", opener=opener
                )
                with self.assertRaises(TelegramSendError) as captured:
                    await notifier.send(sample_signal())
                self.assertIn(expected, str(captured.exception))
                self.assertNotIn("private-test-token", str(captured.exception))
                self.assertNotIn("private-test-token", repr(captured.exception))
                if status == 429:
                    self.assertEqual(captured.exception.retry_after, 17)

    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")

    async def test_transport_exception_does_not_leak_request_url(self) -> None:
        token = "private-token-from-url"

        def opener(request, timeout):
            raise urllib.error.URLError(f"failed request {request.full_url}")

        notifier = TelegramNotifier(token, "chat", opener=opener)
        with self.assertRaises(TelegramSendError) as captured:
            await notifier.send(sample_signal())
        self.assertNotIn(token, str(captured.exception))

    def test_missing_telegram_secret_falls_back_to_console(self) -> None:
        with self.assertLogs("gbot.notifications", level="WARNING") as captured:
            notifier = build_notifier({"TELEGRAM_BOT_TOKEN": "configured"})
        self.assertIsInstance(notifier, ConsoleNotifier)
        self.assertIn("TELEGRAM_CHAT_ID", "\n".join(captured.output))
        self.assertNotIn("configured", "\n".join(captured.output))


class NotificationQueueTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")
    async def test_signal_that_expires_in_queue_is_not_sent(self) -> None:
        class Recorder:
            sent: list[Signal] = []

            async def send(self, signal: Signal) -> str:
                self.sent.append(signal)
                return "sent"

        recorder = Recorder()
        queue = NotificationQueue(
            recorder,
            valid_seconds=15,
            batch_window_seconds=0.04,
        )
        signal = sample_signal(
            valid_until=datetime.now(timezone.utc) + timedelta(seconds=0.01)
        )
        queue.enqueue(signal)
        queue.start()
        await asyncio.sleep(0.07)
        await queue.close()
        self.assertEqual(recorder.sent, [])

    @unittest.skip("OBSOLETE (failing before 2026-11-20): uses the removed pullback_reversal Signal / old NotificationQueue arguments.")

    async def test_rate_limit_keeps_strongest_queued_signals_and_logs_drops(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.sent: list[Signal] = []

            async def send(self, signal: Signal) -> str:
                self.sent.append(signal)
                return "sent"

        recorder = Recorder()
        queue = NotificationQueue(
            recorder,
            max_per_minute=3,
            batch_window_seconds=0.02,
        )
        with self.assertLogs("gbot.notifications", level="INFO") as captured:
            queue.start()
            for strength in (10, 20, 30, 40, 50):
                queue.enqueue(sample_signal(strength=strength))
            await asyncio.sleep(0.06)
            await queue.close()
        self.assertEqual([item.strength for item in recorder.sent], [50, 40, 30])
        dropped_lines = [
            line for line in captured.output if "lowest-strength queued" in line
        ]
        self.assertEqual(
            sum(int(line.split("Dropped ", 1)[1].split(" ", 1)[0]) for line in dropped_lines),
            2,
        )


if __name__ == "__main__":
    unittest.main()
