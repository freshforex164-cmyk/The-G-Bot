"""Controlled regressions for shared socket ownership and async initialization."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from autotrade.clock import Clock
from autotrade.market.feed import SharedMarketFeed
from autotrade.trading.balance_pool import BalancePool, Entry
from autotrade.trading.manager import CapacityError
from autotrade.tests.fakes import Env


class FeedOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_initial_connection_closes_unpublished_socket(self):
        entered = asyncio.Event()
        async def connect():
            entered.set()
            await asyncio.Event().wait()
        broker = SimpleNamespace(connect=connect, close=AsyncMock())
        feed = SharedMarketFeed(lambda: broker)
        task = asyncio.create_task(feed._get_broker())
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(broker.close.await_count, 1)
        self.assertIsNone(feed._broker)

    async def test_old_stream_failure_cannot_close_replacement_socket(self):
        old = SimpleNamespace(close=AsyncMock())
        current = SimpleNamespace(close=AsyncMock())
        feed = SharedMarketFeed(lambda: current)
        feed._broker = current
        await feed._drop_broker(expected=old)
        self.assertIs(feed._broker, current)
        current.close.assert_not_awaited()
        await feed.aclose()


class BalanceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_user_does_not_block_another_users_balance(self):
        e = Env()
        await e.add_user('a')
        await e.add_user('b')
        pool = e.rt.manager._balances
        entered = asyncio.Event()
        release = asyncio.Event()
        async def hold():
            async with pool.connection('a', 'demo'):
                entered.set()
                await release.wait()
        task = asyncio.create_task(hold())
        try:
            await entered.wait()
            async def check_other():
                async with pool.connection('b', 'demo') as broker:
                    return await broker.balance()
            value = await asyncio.wait_for(check_other(), 0.3)
            self.assertEqual(value, 10000.0)
        finally:
            release.set()
            await task
            await e.rt.manager.shutdown()

    async def test_failed_adoption_does_not_lose_socket_ownership(self):
        broker = SimpleNamespace(close=AsyncMock(), is_alive=AsyncMock(return_value=True))
        repo = SimpleNamespace(get_credential=AsyncMock(side_effect=OSError('offline database error')))
        pool = BalancePool(repo, None, None, Clock())
        pool.entries[('a', 'demo')] = Entry(broker, b'offline', pool.clock.monotonic())
        try:
            with self.assertRaises(OSError):
                await pool.take('a', 'demo')
            self.assertTrue(('a', 'demo') in pool.entries or broker.close.await_count == 1)
        finally:
            await pool.close()


class CapacityReservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_starts_cannot_exceed_worker_capacity(self):
        from dataclasses import replace
        e = Env()
        await e.add_user('a')
        await e.add_user('b')
        manager = e.rt.manager
        manager.cfg = replace(e.cfg, max_workers=1)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = manager.health.check
        async def slow_check(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        manager.health.check = slow_check
        a = asyncio.create_task(manager.start('a'))
        await entered.wait()
        b = asyncio.create_task(manager.start('b'))
        await asyncio.sleep(0.03)
        release.set()
        results = await asyncio.gather(a, b, return_exceptions=True)
        try:
            self.assertEqual(sum(isinstance(r, CapacityError) for r in results), 1)
            self.assertEqual(sum(manager.is_running(u) for u in ('a', 'b')), 1)
        finally:
            await manager.shutdown()


if __name__ == '__main__':
    unittest.main()


class AsyncSdkInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_async_factory_bypasses_blocking_wrapper_constructor(self):
        from unittest.mock import patch
        from broker.sdk_async import open_pocket_option
        from BinaryOptionsToolsV2.pocketoption import PocketOptionAsync
        native = SimpleNamespace(shutdown=AsyncMock())
        entered = asyncio.Event()
        release = asyncio.Event()
        async def factory(*args):
            entered.set()
            await release.wait()
            return native
        frame = '42["auth",{"session":"offline-test-session","uid":1,"isDemo":1}]'
        with patch.object(PocketOptionAsync, '__init__', side_effect=AssertionError('blocking constructor used')), \
             patch('BinaryOptionsToolsV2.BinaryOptionsToolsV2.RawPocketOption.create_with_config', side_effect=factory):
            task = asyncio.create_task(open_pocket_option(frame))
            await asyncio.wait_for(entered.wait(), 0.2)
            self.assertFalse(task.done())
            release.set()
            api = await task
            self.assertIs(api.client, native)
            self.assertFalse(api.config.terminal_logging)
            self.assertTrue(hasattr(api, '_candles_many_cache'))

    async def test_late_native_connection_is_closed_after_caller_timeout(self):
        from unittest.mock import patch
        from broker.sdk_async import open_pocket_option
        native = SimpleNamespace(shutdown=AsyncMock())
        release = asyncio.Event()
        async def factory(*args):
            await release.wait()
            return native
        frame = '42["auth",{"session":"offline-test-session","uid":1,"isDemo":0}]'
        with patch('BinaryOptionsToolsV2.BinaryOptionsToolsV2.RawPocketOption.create_with_config', side_effect=factory):
            with self.assertRaises(TimeoutError):
                await open_pocket_option(frame, timeout=0.01)
            release.set()
            for _ in range(20):
                if native.shutdown.await_count:
                    break
                await asyncio.sleep(0.01)
            native.shutdown.assert_awaited_once()


class HealthDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_lock_wait_is_covered_by_health_deadline(self):
        e = Env()
        await e.add_user('a')
        e.rt.health._timeout = 0.03
        pool = e.rt.manager._balances
        entered = asyncio.Event()
        release = asyncio.Event()
        async def hold():
            async with pool.connection('a', 'demo'):
                entered.set()
                await release.wait()
        task = asyncio.create_task(hold())
        await entered.wait()
        try:
            result = await asyncio.wait_for(e.rt.health.check('a', 'demo'), 0.2)
            self.assertEqual(result.status, 'unreachable')
            self.assertEqual((await e.repo.get_credential('a', 'demo')).status, 'active')
        finally:
            release.set()
            await task
            await e.rt.manager.shutdown()
