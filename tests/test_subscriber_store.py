from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from subscriber_store import SubscriberStore


class FakeCursor:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.statements: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params=None):
        self.statements.append((statement, params))
        if "SELECT chat_id" in statement:
            return FakeCursor(self.rows)
        return FakeCursor()


class SubscriberStoreTests(unittest.TestCase):
    def test_postgres_loads_table_and_persists_only_changed_subscription(self) -> None:
        connection = FakeConnection(
            [(123, True, "free", None, None)]
        )
        calls: list[tuple[str, int]] = []

        def connect(url, *, connect_timeout):
            calls.append((url, connect_timeout))
            return connection

        store = SubscriberStore(
            "postgres://private-placeholder",
            connect=connect,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(store.backend, "postgres")
        self.assertEqual(store.subscribed_chat_ids(), [123])
        self.assertFalse(store.set_subscribed(123, True))
        self.assertTrue(store.set_subscribed(123, False))
        self.assertEqual(store.subscribed_chat_ids(), [])
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(timeout == 10 for _url, timeout in calls))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS subscribers" in sql for sql, _ in connection.statements))
        self.assertTrue(any("INSERT INTO subscribers" in sql for sql, _ in connection.statements))

    def test_unreachable_database_falls_back_to_json_without_logging_url(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "subscribers.json"
            waits: list[float] = []

            def unavailable(_url, *, connect_timeout):
                raise ConnectionError("database unavailable")

            with self.assertLogs("gbot.subscribers", level="WARNING") as output:
                store = SubscriberStore(
                    "postgres://private-placeholder",
                    file_path=path,
                    connect=unavailable,
                    sleep=waits.append,
                )
            joined_logs = "\n".join(output.output)
            self.assertNotIn("private-placeholder", joined_logs)
            self.assertIn("JSON fallback", joined_logs)
            self.assertEqual(len(waits), 2)
            self.assertEqual(store.backend, "json")
            self.assertTrue(store.set_subscribed(456, True))
            self.assertFalse(store.set_subscribed(456, True))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["chat_id"], 456)
            self.assertTrue(data[0]["subscribed"])

    def test_database_write_failure_falls_back_and_keeps_loaded_subscribers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "subscribers.json"
            connection = FakeConnection([(123, True, "free", None, None)])
            calls = 0

            def connect(_url, *, connect_timeout):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return connection
                raise ConnectionError("database write unavailable")

            with self.assertLogs("gbot.subscribers", level="WARNING") as output:
                store = SubscriberStore(
                    "postgres://private-placeholder",
                    file_path=path,
                    connect=connect,
                    sleep=lambda _seconds: None,
                )
                self.assertTrue(store.set_subscribed(123, False))

            joined_logs = "\n".join(output.output)
            self.assertNotIn("private-placeholder", joined_logs)
            self.assertIn("JSON fallback", joined_logs)
            self.assertEqual(store.backend, "json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data[0]["chat_id"], 123)
            self.assertFalse(data[0]["subscribed"])

    def test_missing_database_uses_json_storage(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with self.assertLogs("gbot.subscribers", level="WARNING"):
                path = Path(temp_dir) / "subscribers.json"
                store = SubscriberStore(
                    database_url="",
                    file_path=path,
                )
            self.assertEqual(store.backend, "json")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_existing_json_fallback_is_not_overwritten_at_startup(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "subscribers.json"
            expected = [{"chat_id": 77, "subscribed": True, "plan": "free"}]
            path.write_text(json.dumps(expected), encoding="utf-8")
            with self.assertLogs("gbot.subscribers", level="WARNING"):
                store = SubscriberStore(database_url="", file_path=path)
            self.assertEqual(store.subscribed_chat_ids(), [77])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)


if __name__ == "__main__":
    unittest.main()
