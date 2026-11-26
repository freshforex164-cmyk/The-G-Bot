"""Transaction-local timeout configuration without a live Neon connection."""
import os
import unittest
from unittest.mock import AsyncMock, patch
from autotrade.storage.postgres import PostgresRepository


class DatabaseTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_apply_transaction_local_bounded_waits(self):
        with patch.dict(os.environ, {}, clear=True), patch('psycopg_pool.AsyncConnectionPool'):
            repo = PostgresRepository('dbname=offline')
        conn = AsyncMock()
        await repo._set_timeouts(conn)
        query, values = conn.execute.call_args.args
        self.assertEqual(values, ('15000', '5000', '30000'))
        self.assertIn("current_setting('statement_timeout')", query)
        self.assertIn("else current_setting('statement_timeout')", query)
        self.assertIn('end, true)', query)

    def test_operator_can_set_explicit_timeout_values(self):
        env = {'AUTOTRADE_DB_STATEMENT_TIMEOUT_MS': '20000', 'AUTOTRADE_DB_LOCK_TIMEOUT_MS': '2000',
               'AUTOTRADE_DB_IDLE_TRANSACTION_TIMEOUT_MS': '40000'}
        with patch.dict(os.environ, env, clear=True), patch('psycopg_pool.AsyncConnectionPool'):
            repo = PostgresRepository('dbname=offline')
        self.assertEqual(repo._sql_timeouts, ('20000', '2000', '40000'))

    def test_invalid_database_timeout_is_rejected_before_connecting(self):
        with patch.dict(os.environ, {'AUTOTRADE_DB_LOCK_TIMEOUT_MS': '-1'}, clear=True):
            with self.assertRaises(ValueError):
                PostgresRepository('dbname=offline')
