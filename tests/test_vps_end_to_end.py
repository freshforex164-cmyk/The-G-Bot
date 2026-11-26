"""Real local polling/command/health flow with only external providers replaced."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main
from admin_http import AdminHttpServer
from data.signals import SignalOutcomeTracker
from subscriber_store import SubscriberStore
from telegram_bot import TelegramBotClient, TelegramCommandHandler


class Response(io.BytesIO):
    status = 200
    def getcode(self):
        return 200


class VpsEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_command_and_health_work_during_broker_outage_until_cancelled(self):
        calls = []
        polls = []
        servers = []
        replied = asyncio.Event()
        saved = asyncio.Event()
        loop = asyncio.get_running_loop()
        def opener(request, timeout):
            method = request.full_url.rsplit('/', 1)[-1]
            payload = json.loads(request.data)
            calls.append((method, payload))
            result = True
            if method == 'getUpdates':
                polls.append(payload)
                time.sleep(.015)
                result = ([{'update_id': 10, 'message': {'chat': {'id': 101, 'type': 'private'},
                    'from': {'id': 101}, 'text': '/start'}},
                    {'update_id': 11, 'message': {'chat': {'id': 101, 'type': 'private'},
                    'from': {'id': 101}, 'text': '/stop'}}] if len(polls) == 1 else [])
            elif method == 'sendMessage':
                result = {'message_id': 55}
                if payload['chat_id'] == 101:
                    loop.call_soon_threadsafe(replied.set)
            elif method == 'editMessageText':
                loop.call_soon_threadsafe(saved.set)
            return Response(json.dumps({'ok': True, 'result': result}).encode())
        def server_factory(**kwargs):
            kwargs['port'] = 0
            server = AdminHttpServer(**kwargs)
            servers.append(server)
            return server
        with tempfile.TemporaryDirectory() as folder:
            store = SubscriberStore(database_url='', file_path=Path(folder)/'subscribers.json')
            outcomes = SignalOutcomeTracker(Path(folder)/'signals.csv')
            brokers = []
            def broker_factory(ssid):
                broker = SimpleNamespace(is_connected=False,
                    connect=AsyncMock(side_effect=TimeoutError('offline outage')), close=AsyncMock())
                brokers.append(broker)
                return broker
            with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'offline-only', 'TELEGRAM_ADMIN_ID': '900',
                    'PO_SSID': '42["auth",{"session":"offline-only","isDemo":1}]'}, clear=True), \
                 patch.object(main, 'TelegramBotClient', return_value=TelegramBotClient('offline-only', opener=opener)), \
                 patch.object(main, 'PocketOptionBroker', side_effect=broker_factory), \
                 patch.object(main, 'SubscriberStore', return_value=store), \
                 patch.object(main, 'SignalOutcomeTracker', return_value=outcomes), \
                 patch.object(main, 'AdminHttpServer', side_effect=server_factory), \
                 patch.object(main.config, 'INITIAL_CONNECT_TIMEOUT_SECONDS', .01):
                task = asyncio.create_task(main.run_serve())
                try:
                    await asyncio.wait_for(replied.wait(), 2)
                    await asyncio.wait_for(saved.wait(), 2)
                    server = servers[0].server
                    self.assertIsNotNone(server)
                    port = server.sockets[0].getsockname()[1]
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.write(b'GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n')
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(), 1)
                    writer.close()
                    await writer.wait_closed()
                    self.assertTrue(response.startswith(b'HTTP/1.1 200'))
                    health = json.loads(response.split(b'\r\n\r\n', 1)[1])
                    self.assertTrue(health['ok'])
                    self.assertFalse(health['feed_connected'])
                    await asyncio.sleep(.05)
                    self.assertFalse(task.done())
                    self.assertGreaterEqual(len(polls), 2)
                    self.assertEqual(polls[1]['offset'], 12)
                    self.assertIn(('deleteWebhook', {'drop_pending_updates': False}), calls)
                    self.assertFalse(any(method == 'sendPhoto' for method, _ in calls))
                    self.assertIn(101, store.subscribers)
                    self.assertEqual(json.loads(store.file_path.read_text())[0]['chat_id'], 101)
                    self.assertFalse(store.subscribers[101].subscribed)
                finally:
                    task.cancel()
                    result = await asyncio.gather(task, return_exceptions=True)
                    self.assertIsInstance(result[0], asyncio.CancelledError)
                self.assertIsNone(servers[0].server)
                for broker in brokers:
                    broker.close.assert_awaited_once_with()

    async def test_operator_supplied_welcome_image_is_still_used(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'WELCOME_IMAGE_URL': 'https://bot.example.invalid/welcome.jpg'}, clear=True):
            store = SubscriberStore(database_url='', file_path=Path(folder)/'subscribers.json')
            client = SimpleNamespace(send_photo=AsyncMock(return_value={'message_id': 42}), send_message=AsyncMock())
            handler = TelegramCommandHandler(client, store, admin_id=900)
            await handler.handle_update({'message': {'chat': {'id': 101}, 'from': {'id': 101}, 'text': '/start'}})
            self.assertEqual(client.send_photo.await_args.args[1], 'https://bot.example.invalid/welcome.jpg')
            client.send_message.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
