"""Verify scanner resource reset and the effective supplied service target offline."""
import asyncio
import configparser
from pathlib import Path
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from scanner import SignalScanner

ROOT = Path(__file__).resolve().parents[1]


class ScannerResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_start_closes_broker_and_clears_streams_and_readiness(self):
        broker = SimpleNamespace(connect=AsyncMock(side_effect=TimeoutError), close=AsyncMock())
        scanner = SignalScanner(broker, SimpleNamespace(subscribers={}), output=lambda line: None)
        scanner.pairs['offline'] = object()
        scanner._ready.set()
        with self.assertRaises(TimeoutError):
            await scanner.run()
        self.assertEqual(scanner.pairs, {})
        self.assertFalse(scanner._ready.is_set())
        broker.close.assert_awaited_once_with()

    async def test_successful_retry_resubscribes_selected_assets_with_fresh_broker(self):
        broker = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(),
            active_assets=AsyncMock(return_value=[]), payouts=AsyncMock(return_value={}),
            monitor_connection=AsyncMock(side_effect=self.forever))
        scanner = SignalScanner(broker, SimpleNamespace(subscribers={}), output=lambda line: None)
        scanner._ready.set()
        async def connect():
            self.assertFalse(scanner._ready.is_set())
        broker.connect.side_effect = connect
        await scanner.run(minutes=.001)
        broker.connect.assert_awaited_once_with()
        broker.close.assert_awaited_once_with()
        self.assertFalse(scanner._ready.is_set())

    async def forever(self, stop):
        await stop.wait()


class SystemdExecutionTests(unittest.TestCase):
    def test_unit_executes_existing_persistent_target_and_restarts_without_cutoff(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / 'deploy/gbot.service')
        command = shlex.split(unit['Service']['ExecStart'])
        self.assertEqual(command, ['/opt/The-G-Bot/.venv/bin/python', '-u',
                                   '/opt/The-G-Bot/main.py', '--run', 'serve'])
        self.assertTrue((ROOT / Path(command[2]).name).is_file())
        self.assertEqual(unit['Service']['Restart'], 'always')
        self.assertEqual(unit['Unit']['StartLimitIntervalSec'], '0')
        self.assertEqual(unit['Service']['EnvironmentFile'], '/opt/The-G-Bot/.env')


if __name__ == '__main__':
    unittest.main()
