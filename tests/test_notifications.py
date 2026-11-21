from __future__ import annotations

import json
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from notifications import (
    ConsoleNotifier,
    TelegramNotifier,
    TelegramSendError,
    build_notifier,
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


    def test_missing_telegram_secret_falls_back_to_console(self) -> None:
        with self.assertLogs("gbot.notifications", level="WARNING") as captured:
            notifier = build_notifier({"TELEGRAM_BOT_TOKEN": "configured"})
        self.assertIsInstance(notifier, ConsoleNotifier)
        self.assertIn("TELEGRAM_CHAT_ID", "\n".join(captured.output))
        self.assertNotIn("configured", "\n".join(captured.output))



if __name__ == "__main__":
    unittest.main()
