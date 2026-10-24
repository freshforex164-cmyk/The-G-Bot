from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

import config
from notifications import TelegramSendError
from subscriber_store import SubscriberStore
from telegram_bot import (
    SubscriberFanoutNotifier,
    TelegramBotClient,
    TelegramCommandHandler,
    TelegramPollingService,
)
from strategy.pullback_reversal import Signal


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int | str, str, dict | None, str | None]] = []
        self.callbacks: list[str] = []
        self.updates: list[list[dict]] = []
        self.offsets: list[int | None] = []

    async def send_message(
        self,
        chat_id,
        text,
        *,
        reply_markup=None,
        parse_mode="HTML",
    ) -> None:
        self.messages.append((chat_id, text, reply_markup, parse_mode))

    async def answer_callback(self, callback_id: str) -> None:
        self.callbacks.append(callback_id)

    async def get_updates(self, *, offset, poll_timeout=25):
        self.offsets.append(offset)
        if self.updates:
            return self.updates.pop(0)
        return []


def message_update(
    command: str,
    *,
    chat_id: int = 101,
    sender_id: int = 101,
) -> dict:
    return {
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": sender_id},
            "text": command,
        }
    }


class TelegramCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.store = SubscriberStore(
            database_url="",
            file_path=Path(self.temp_dir.name) / "subscribers.json",
        )
        self.client = FakeClient()
        self.handler = TelegramCommandHandler(
            self.client,
            self.store,
            admin_id=900,
            admin_chat_id=-100900,
            referral_link="https://example.test/join?src=gbot&tag=one",
            scanner_running=lambda: True,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_start_subscribes_and_shows_only_stop_and_status(self) -> None:
        await self.handler.handle_update(message_update("/start@gbot"))
        self.assertEqual(self.store.subscribed_chat_ids(), [101])
        chat_id, text, markup, _ = self.client.messages[-1]
        self.assertEqual(chat_id, 101)
        self.assertEqual(text, "Signals are on for this chat")
        self.assertEqual(
            [row[0]["text"] for row in markup["inline_keyboard"]],
            ["Stop signals", "Status"],
        )

    async def test_start_again_is_idempotent_and_does_not_repeat_welcome(self) -> None:
        await self.handler.handle_update(message_update("/start"))
        await self.handler.handle_update(message_update("/start"))
        self.assertEqual(self.store.subscribed_chat_ids(), [101])
        self.assertEqual(self.client.messages[-1][1], "Signals are already on")
        self.assertEqual(
            [row[0]["text"] for row in self.client.messages[-1][2]["inline_keyboard"]],
            ["Stop signals", "Status"],
        )

    async def test_stop_unsubscribes_and_status_reports_state(self) -> None:
        await self.handler.handle_update(message_update("/start"))
        await self.handler.handle_update(message_update("/stop"))
        self.assertEqual(self.store.subscribed_chat_ids(), [])
        self.assertEqual(self.client.messages[-1][1], "Signals are off")
        self.assertEqual(
            [row[0]["text"] for row in self.client.messages[-1][2]["inline_keyboard"]],
            ["Start signals", "Register"],
        )
        await self.handler.handle_update(message_update("/status"))
        self.assertIn("Signals: off", self.client.messages[-1][1])
        self.assertIn("Scanner: running", self.client.messages[-1][1])
        self.assertIn(f"Minimum payout: {config.MIN_PAYOUT}%", self.client.messages[-1][1])

    async def test_stop_again_is_idempotent(self) -> None:
        await self.handler.handle_update(message_update("/stop"))
        self.assertEqual(self.client.messages[-1][1], "Signals are off")
        await self.handler.handle_update(message_update("/stop"))
        self.assertEqual(self.client.messages[-1][1], "Signals are already off")
        self.assertEqual(self.store.subscribed_chat_ids(), [])

    async def test_register_injects_referral_link_and_start_button(self) -> None:
        await self.handler.handle_update(message_update("/register"))
        _chat_id, text, markup, _mode = self.client.messages[-1]
        self.assertIn("https://example.test/join?src=gbot&amp;tag=one", text)
        self.assertEqual(
            markup["inline_keyboard"],
            [
                [{"text": "Start signals", "callback_data": "signals:start"}],
                [{"text": "Register", "callback_data": "signals:register"}],
            ],
        )

    async def test_register_without_referral_omits_url_button(self) -> None:
        with self.assertLogs("gbot.telegram", level="WARNING"):
            handler = TelegramCommandHandler(
                self.client,
                self.store,
                admin_id=900,
                referral_link=None,
            )
            await handler.handle_update(message_update("/register"))
        _chat_id, text, markup, _mode = self.client.messages[-1]
        self.assertIn("not configured", text)
        self.assertNotIn("url", str(markup))

    async def test_help_describes_information_only_signals(self) -> None:
        await self.handler.handle_update(message_update("/help"))
        text = self.client.messages[-1][1]
        self.assertIn("How GBot works", text)
        self.assertIn("place the trade quickly", text)
        self.assertIn("Strength shows how strong the setup is", text)
        self.assertIn("not financial advice", text)
        self.assertIn("not a guarantee", text)

    async def test_admin_command_is_restricted_to_configured_user(self) -> None:
        await self.handler.handle_update(
            message_update("/admin", chat_id=-100900, sender_id=101)
        )
        self.assertEqual(self.client.messages, [])
        await self.handler.handle_update(
            message_update("/admin", chat_id=101, sender_id=900)
        )
        self.assertEqual(self.client.messages, [])
        await self.handler.handle_update(
            message_update("/admin", chat_id=-100900, sender_id=900)
        )
        report = self.client.messages[-1][1]
        self.assertIn("Subscribers: 0", report)
        self.assertIn("Signals today: 0", report)
        self.assertIn("Paper win rate", report)
        self.assertIn("Tick-to-signal latency", report)
        self.assertIn("Signal-to-Telegram latency", report)
        self.assertIn("SSID:", report)
        self.assertNotIn("42[", report)

    async def test_admin_health_pause_resume_are_restricted_to_admin_chat(self) -> None:
        class FakeScanner:
            is_paused = False

            def pause(self):
                self.is_paused = True

            def resume(self):
                self.is_paused = False

        scanner = FakeScanner()
        self.handler.scanner = scanner
        await self.handler.handle_update(
            message_update("/pause", chat_id=-100900, sender_id=101)
        )
        await self.handler.handle_update(
            message_update("/pause", chat_id=101, sender_id=900)
        )
        self.assertFalse(scanner.is_paused)
        self.assertEqual(self.client.messages, [])

        await self.handler.handle_update(
            message_update("/pause", chat_id=-100900, sender_id=900)
        )
        self.assertTrue(scanner.is_paused)
        self.assertIn("paused", self.client.messages[-1][1])
        await self.handler.handle_update(
            message_update("/health", chat_id=-100900, sender_id=900)
        )
        self.assertIn("Feed:", self.client.messages[-1][1])
        await self.handler.handle_update(
            message_update("/resume", chat_id=-100900, sender_id=900)
        )
        self.assertFalse(scanner.is_paused)
        self.assertIn("resumed", self.client.messages[-1][1])

    async def test_subscribers_command_shows_only_active_chats_in_admin_chat(self) -> None:
        self.store.set_subscribed(555, True)
        self.store.set_subscribed(556, False)
        await self.handler.handle_update(
            message_update("/subscribers", chat_id=-100900, sender_id=900)
        )
        chat_id, text, _markup, _mode = self.client.messages[-1]
        self.assertEqual(chat_id, -100900)
        self.assertIn("Active subscribers: 1", text)
        self.assertIn("555", text)
        self.assertNotIn("556", text)

    async def test_testsignal_goes_only_to_admin_chat_with_test_label(self) -> None:
        await self.handler.handle_update(
            message_update("/testsignal", chat_id=101, sender_id=900)
        )
        self.assertEqual(self.client.messages, [])
        await self.handler.handle_update(
            message_update("/testsignal", chat_id=-100900, sender_id=900)
        )
        chat_id, text, _markup, _mode = self.client.messages[-1]
        self.assertEqual(chat_id, -100900)
        self.assertEqual(len(text.splitlines()), 6)
        self.assertIn("<b>SELL</b>", text)
        self.assertIn("Asset: EUR/GBP OTC", text)
        self.assertIn("Expiration: <b>35s</b>", text)
        self.assertEqual(text.splitlines()[-1], "<b>TEST</b>")

    async def test_callback_buttons_answer_query_and_change_subscription(self) -> None:
        await self.handler.handle_update(
            {
                "callback_query": {
                    "id": "callback-1",
                    "from": {"id": 101},
                    "data": "signals:start",
                    "message": {"chat": {"id": 101}},
                }
            }
        )
        self.assertEqual(self.client.callbacks, ["callback-1"])
        self.assertEqual(self.store.subscribed_chat_ids(), [101])
        self.assertEqual(self.client.messages[-1][1], "Signals are on for this chat")
        self.assertEqual(
            [row[0]["text"] for row in self.client.messages[-1][2]["inline_keyboard"]],
            ["Stop signals", "Status"],
        )

    async def test_unrecognized_callback_is_still_acknowledged(self) -> None:
        await self.handler.handle_update(
            {
                "callback_query": {
                    "id": "callback-unknown",
                    "data": "other:action",
                    "message": {"chat": {"id": 101}},
                }
            }
        )
        self.assertEqual(self.client.callbacks, ["callback-unknown"])
        self.assertEqual(self.client.messages, [])

    async def test_subscription_persists_across_store_restart(self) -> None:
        await self.handler.handle_update(message_update("/start"))
        restarted = SubscriberStore(
            database_url="",
            file_path=Path(self.temp_dir.name) / "subscribers.json",
        )
        self.assertEqual(restarted.subscribed_chat_ids(), [101])
        restarted.set_subscribed(101, False)
        reloaded = SubscriberStore(
            database_url="",
            file_path=Path(self.temp_dir.name) / "subscribers.json",
        )
        self.assertEqual(reloaded.subscribed_chat_ids(), [])


class TelegramApiAndPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_registration_sets_default_and_admin_scopes(self) -> None:
        requests: list[dict] = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"ok": true, "result": true}'

            def getcode(self):
                return self.status

        def opener(request, timeout):
            requests.append(json.loads(request.data))
            return Response()

        client = TelegramBotClient("mock-token-never-log", opener=opener)
        await client.register_commands(admin_id=900)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["scope"], {"type": "default"})
        self.assertEqual(requests[0]["commands"][-1]["command"], "register")
        self.assertEqual(
            requests[1]["scope"], {"type": "chat", "chat_id": 900}
        )
        self.assertIn("testsignal", [item["command"] for item in requests[1]["commands"]])

    async def test_poller_advances_offset_and_sends_it_on_next_request(self) -> None:
        client = FakeClient()
        client.updates = [
            [{"update_id": 45, **message_update("/help")}],
            [{"update_id": 47, **message_update("/help")}],
        ]
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            handler = TelegramCommandHandler(client, store, admin_id=900)
            poller = TelegramPollingService(client, handler)
            self.assertEqual(await poller.poll_once(), 1)
            self.assertEqual(poller.offset, 46)
            self.assertEqual(await poller.poll_once(), 1)
            self.assertEqual(poller.offset, 48)
        self.assertEqual(client.offsets, [None, 46])


class SubscriberFanoutTests(unittest.IsolatedAsyncioTestCase):
    def make_signal(self) -> Signal:
        now = datetime.now(timezone.utc)
        return Signal(
            pair="EURGBP_otc",
            direction="SELL",
            price=1.0,
            time=now,
            stochastic_k=70,
            stochastic_d=65,
            ma_slope=-0.01,
            bollinger_middle=1.0,
            payout=92,
            strength=84,
            recommended_expiry_seconds=35,
            valid_until=now + timedelta(seconds=30),
            asset_name="EUR/GBP OTC",
        )

    async def test_403_removes_chat_and_other_subscribers_receive_signal(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            store.set_subscribed(1, True)
            store.set_subscribed(2, True)

            class Client(FakeClient):
                async def send_message(self, chat_id, text, **kwargs):
                    if chat_id == 1:
                        raise TelegramSendError(
                            "forbidden", status_code=403
                        )
                    await super().send_message(chat_id, text, **kwargs)

            client = Client()
            fanout = SubscriberFanoutNotifier(
                client, store, max_messages_per_second=1000
            )
            await fanout.send(self.make_signal())
            self.assertEqual(store.subscribed_chat_ids(), [2])
            self.assertEqual([item[0] for item in client.messages], [2])

    async def test_429_waits_retry_after_and_retries_delivery(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = SubscriberStore(
                database_url="",
                file_path=Path(temp_dir) / "subscribers.json",
            )
            store.set_subscribed(2, True)
            waits: list[float] = []

            async def fake_sleep(seconds: float) -> None:
                waits.append(seconds)

            class Client(FakeClient):
                def __init__(self):
                    super().__init__()
                    self.calls = 0

                async def send_message(self, chat_id, text, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise TelegramSendError(
                            "rate limited",
                            retry_after=2,
                            status_code=429,
                        )
                    await super().send_message(chat_id, text, **kwargs)

            client = Client()
            fanout = SubscriberFanoutNotifier(
                client, store, max_messages_per_second=1000, sleep=fake_sleep
            )
            await fanout.send(self.make_signal())
            self.assertEqual(client.calls, 2)
            self.assertIn(2.0, waits)
            self.assertEqual(len(client.messages), 1)


if __name__ == "__main__":
    unittest.main()
