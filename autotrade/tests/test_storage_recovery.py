"""Offline SSL-drop regressions; no production credentials or database needed."""
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import psycopg
from psycopg.conninfo import conninfo_to_dict

from ..storage.postgres import PostgresRepository


class ScriptedPool:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0
        self.conn = AsyncMock()
        self.conn.execute.return_value.fetchall.return_value = [(17,)]

    @asynccontextmanager
    async def connection(self):
        self.calls += 1
        failure = self.failures.pop(0) if self.failures else None
        if failure:
            raise failure
        yield self.conn


class StorageRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def repository(self, failures):
        repo = PostgresRepository("dbname=offline")
        repo._pool = ScriptedPool(failures)
        return repo

    def test_pool_checks_connections_preserves_ssl_and_sets_keepalives(self):
        with patch("psycopg_pool.AsyncConnectionPool") as pool:
            PostgresRepository("dbname=offline sslmode=verify-full connect_timeout=7")
        args, kwargs = pool.call_args
        options = conninfo_to_dict(args[0])
        self.assertEqual(options["sslmode"], "verify-full")
        self.assertEqual(options["connect_timeout"], "7")
        self.assertEqual([options[k] for k in ("keepalives", "keepalives_idle", "keepalives_interval", "keepalives_count")],
                         ["1", "30", "10", "3"])
        self.assertEqual(kwargs["check"], pool.check_connection)
        self.assertEqual(kwargs["max_idle"], 60)

    async def test_ssl_read_drop_retries_on_new_checkout(self):
        repo = self.repository([psycopg.OperationalError("SSL closed"), None])
        self.assertEqual(await repo._all("select 17"), [(17,)])
        self.assertEqual(repo._pool.calls, 2)

    async def test_retries_are_bounded(self):
        repo = self.repository([psycopg.OperationalError("SSL closed")] * 4)
        with self.assertRaises(psycopg.OperationalError):
            await repo._all("select 17")
        self.assertEqual(repo._pool.calls, 3)

    async def test_integrity_errors_are_not_retried(self):
        repo = self.repository([psycopg.IntegrityError("invalid")])
        with self.assertRaises(psycopg.IntegrityError):
            await repo._all("select 17")
        self.assertEqual(repo._pool.calls, 1)

    async def test_ambiguous_write_is_never_replayed(self):
        repo = self.repository([psycopg.OperationalError("lost commit acknowledgement")])
        with self.assertRaises(psycopg.OperationalError):
            await repo._exec("insert into autotrade_trades values (%s)", ("ref",))
        self.assertEqual(repo._pool.calls, 1)