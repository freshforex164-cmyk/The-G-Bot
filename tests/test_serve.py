from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class ServeTests(unittest.IsolatedAsyncioTestCase):
    async def test_serve_starts_scanner_telegram_and_http_without_external_calls(self):
        instances = {}
        scanner_ready = asyncio.Event()

        class FakeBot:
            def __init__(self, _token):
                pass

            async def register_commands(self, admin_id):
                instances["registered_chat"] = admin_id

            async def send_message(self, *_args, **_kwargs):
                return None

        class FakeStore:
            subscribers = {}

            def count(self):
                return 0

            def subscribed_chat_ids(self):
                return []

            def set_subscribed(self, *_args):
                return False

        class FakeTracker:
            records = {}

            def __init__(self, **_kwargs):
                pass

            def record_signal(self, _signal):
                pass

            def update_price(self, *_args):
                pass

            def print_summary(self):
                instances["summary_printed"] = True

        class FakeDecisionStore:
            def __init__(self):
                pass

            def record_candle(self, *_args):
                pass

            def print_summary(self):
                instances["decision_summary_printed"] = True

        class FakeQueue:
            def __init__(self, *_args, **_kwargs):
                self.closed = False
                instances["queue"] = self

            def start(self):
                instances["queue_started"] = True

            def enqueue(self, _signal):
                pass

            async def close(self):
                self.closed = True

        class FakeScanner:
            def __init__(self, *_args, **_kwargs):
                self.started = asyncio.Event()
                self.stop = asyncio.Event()
                self.broker = SimpleNamespace(is_connected=False)
                self.pairs = {}
                self.signals = {}
                self.active_signals = []
                self.last_tick_age_seconds = None
                self.last_signal_latency_ms = None
                self.is_paused = False
                instances["scanner"] = self
                scanner_ready.set()

            async def run(self, *, minutes):
                self.assert_minutes = minutes
                self.started.set()
                await self.stop.wait()

            def pause(self):
                self.is_paused = True

            def resume(self):
                self.is_paused = False

        class FakePolling:
            def __init__(self, *_args, **_kwargs):
                self.started = asyncio.Event()
                self.stop = asyncio.Event()
                instances["polling"] = self

            async def run(self, _stop_event):
                self.started.set()
                await self.stop.wait()

        class FakeHttp:
            def __init__(self, **kwargs):
                self.health_provider = kwargs["health_provider"]
                self.admin_provider = kwargs["admin_provider"]
                self.admin_token = kwargs["admin_token"]
                self.started = asyncio.Event()
                self.stop = asyncio.Event()
                self.closed = False
                instances["http"] = self

            async def start(self):
                return SimpleNamespace(
                    sockets=[
                        SimpleNamespace(
                            getsockname=lambda: ("0.0.0.0", 5000)
                        )
                    ]
                )

            async def serve_forever(self):
                self.started.set()
                await self.stop.wait()

            async def close(self):
                self.closed = True
                self.stop.set()

        with (
            patch.dict(
                "os.environ",
                {
                    "TELEGRAM_BOT_TOKEN": "mock-telegram-token",
                    "TELEGRAM_ADMIN_ID": "123",
                    "TELEGRAM_USER_ID": "456",
                    "TELEGRAM_CHAT_ID": "-100123",
                    "ADMIN_TOKEN": "mock-admin-token",
                    "PORT": "5000",
                    "DATABASE_URL": "",
                    "REFERRAL_LINK": "",
                },
            ),
            patch.object(main, "TelegramBotClient", FakeBot),
            patch.object(main, "SubscriberStore", FakeStore),
            patch.object(main, "PaperTracker", FakeTracker),
            patch.object(main, "DecisionStore", FakeDecisionStore),
            patch.object(main, "NotificationQueue", FakeQueue),
            patch.object(main, "SignalScanner", FakeScanner),
            patch.object(main, "TelegramPollingService", FakePolling),
            patch.object(main, "AdminHttpServer", FakeHttp),
            patch.object(main, "PocketOptionBroker", lambda _ssid: object()),
            patch.object(main, "load_ssid", return_value="mock-ssid"),
        ):
            serve_task = asyncio.create_task(main.run_serve())
            await asyncio.wait_for(scanner_ready.wait(), timeout=2)
            await asyncio.wait_for(
                asyncio.gather(
                    instances["scanner"].started.wait(),
                    instances["polling"].started.wait(),
                    instances["http"].started.wait(),
                ),
                timeout=2,
            )
            health = instances["http"].health_provider()
            admin_status = instances["http"].admin_provider()
            self.assertEqual(health["scanner"], "running")
            self.assertEqual(instances["http"].admin_token, "mock-admin-token")
            self.assertEqual(admin_status["subscriber_count"], 0)
            self.assertEqual(instances["scanner"].assert_minutes, None)
            self.assertEqual(
                instances["registered_chat"],
                123,
                "The admin command scope must use the admin user ID.",
            )
            self.assertTrue(instances["queue_started"])

            serve_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await serve_task

        self.assertTrue(instances["http"].closed)
        self.assertTrue(instances["queue"].closed)
        self.assertTrue(instances["summary_printed"])


if __name__ == "__main__":
    unittest.main()
