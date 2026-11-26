"""Offline VPS lifetime regressions; never use real broker or Telegram credentials."""
import asyncio
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from notifications import TelegramSendError
from telegram_bot import TelegramBotClient, TelegramPollingService


class PersistentCliTests(unittest.TestCase):
    def test_no_arguments_selects_persistent_service_not_finite_watch(self):
        with patch.object(main, 'run_serve', new_callable=AsyncMock) as serve, \
             patch.object(main, 'run_watch', new_callable=AsyncMock) as watch:
            self.assertEqual(main.main([]), 0)
        serve.assert_awaited_once_with()
        watch.assert_not_awaited()

    def test_explicit_watch_remains_a_finite_diagnostic(self):
        with patch.object(main, 'run_watch', new_callable=AsyncMock, return_value=True) as watch:
            self.assertEqual(main.main(['--run', 'watch', '--seconds', '2']), 0)
        watch.assert_awaited_once_with(seconds=2)


class PollingLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_is_deleted_without_dropping_pending_updates(self):
        client = TelegramBotClient('offline-only')
        with patch.object(client, 'call', new_callable=AsyncMock, return_value=True) as call:
            await client.delete_webhook()
        call.assert_awaited_once_with('deleteWebhook', {'drop_pending_updates': False})

    async def test_polling_retries_network_failure_and_keeps_polling_empty_results(self):
        stop = asyncio.Event()
        calls = []
        async def get_updates(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TelegramSendError('offline outage')
            if len(calls) == 4:
                stop.set()
            await asyncio.sleep(0)
            return []
        client = SimpleNamespace(delete_webhook=AsyncMock(), get_updates=get_updates)
        poller = TelegramPollingService(client, SimpleNamespace(handle_update=AsyncMock()), min_backoff=.001)
        await asyncio.wait_for(poller.run(stop), 1)
        self.assertEqual(len(calls), 4)
        self.assertEqual([c['poll_timeout'] for c in calls], [25] * 4)
        client.delete_webhook.assert_awaited_once_with()

    async def test_webhook_cleanup_retries_before_first_poll(self):
        stop = asyncio.Event()
        events = []
        async def delete_webhook():
            events.append('delete')
            if len(events) == 1:
                raise TelegramSendError('offline outage')
        async def get_updates(**kwargs):
            events.append('poll')
            stop.set()
            return []
        poller = TelegramPollingService(SimpleNamespace(delete_webhook=delete_webhook, get_updates=get_updates),
                                        SimpleNamespace(handle_update=AsyncMock()), min_backoff=.001)
        await asyncio.wait_for(poller.run(stop), 1)
        self.assertEqual(events, ['delete', 'delete', 'poll'])

    async def test_invalid_token_is_fatal_not_an_infinite_retry(self):
        client = SimpleNamespace(delete_webhook=AsyncMock(side_effect=TelegramSendError('invalid', status_code=401)),
                                 get_updates=AsyncMock())
        with self.assertRaises(TelegramSendError):
            await asyncio.wait_for(TelegramPollingService(client, None).run(asyncio.Event()), .2)
        client.get_updates.assert_not_awaited()


class ServeLifetimeTests(unittest.IsolatedAsyncioTestCase):
    def service_fakes(self, *, scan_error=False, poll_returns=False):
        self.polling_started = asyncio.Event()
        self.scans = 0
        async def scan():
            self.scans += 1
            if scan_error:
                raise TimeoutError('offline broker outage')
            await asyncio.Event().wait()
        async def poll(stop):
            self.polling_started.set()
            if not poll_returns:
                await stop.wait()
        self.scanner = SimpleNamespace(run=scan, wait_until_ready=AsyncMock(side_effect=TimeoutError),
            is_paused=False, broker=SimpleNamespace(is_connected=False), pairs={}, active_signals=[])
        self.server = SimpleNamespace(start=AsyncMock(), serve_forever=AsyncMock(side_effect=self.forever), close=AsyncMock())
        self.queue = SimpleNamespace(start=lambda: None, close=AsyncMock())
        self.bot = SimpleNamespace(register_commands=AsyncMock(), send_message=AsyncMock())
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'offline-only', 'TELEGRAM_ADMIN_ID': '900'}, clear=True))
        for name, fake in {'TelegramBotClient':self.bot, 'SignalScanner':self.scanner,
            'SubscriberStore':SimpleNamespace(backend='file', count=lambda: 0),
            'SubscriberFanoutNotifier':SimpleNamespace(), 'NotificationQueue':self.queue,
            'SignalOutcomeTracker':SimpleNamespace(on_live_tick=None, print_summary=lambda: None),
            'PocketOptionBroker':SimpleNamespace(is_connected=False), 'TelegramCommandHandler':SimpleNamespace(),
            'TelegramPollingService':SimpleNamespace(run=poll), 'AdminHttpServer':self.server}.items():
            stack.enter_context(patch.object(main, name, return_value=fake))
        stack.enter_context(patch.object(main.AutotradeClient, 'from_env', return_value=None))
        stack.enter_context(patch.object(main.config, 'load_ssid', return_value='offline-only'))
        return stack

    async def forever(self):
        await asyncio.Event().wait()

    async def test_broker_startup_timeout_does_not_stop_telegram(self):
        with self.service_fakes(scan_error=True):
            task = asyncio.create_task(main.run_serve())
            try:
                await asyncio.wait_for(self.polling_started.wait(), 1)
                await asyncio.sleep(.03)
                self.assertFalse(task.done(), 'broker startup must not terminate polling')
                self.assertGreaterEqual(self.scans, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.server.close.assert_awaited_once_with()
        self.queue.close.assert_awaited_once_with()

    async def test_feed_retry_replaces_closed_broker_while_polling_stays_alive(self):
        with self.service_fakes(scan_error=True):
            original = self.scanner.broker
            second_scan = asyncio.Event()
            async def scan():
                self.scans += 1
                if self.scans == 1:
                    raise TimeoutError('offline outage')
                second_scan.set()
                await asyncio.Event().wait()
            self.scanner.run = scan
            task = asyncio.create_task(main.run_serve())
            try:
                await asyncio.wait_for(second_scan.wait(), 2)
                self.assertEqual(self.scans, 2)
                self.assertIsNot(self.scanner.broker, original)
                self.assertTrue(self.polling_started.is_set())
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_unexpected_clean_poll_exit_is_detected(self):
        with self.service_fakes(poll_returns=True):
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(main.run_serve(), .5)
        self.server.close.assert_awaited_once_with()


if __name__ == '__main__':
    unittest.main()
