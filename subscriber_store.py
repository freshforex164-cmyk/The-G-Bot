"""Persistent Telegram subscriber state with a JSON fallback."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg

logger = logging.getLogger("gbot.subscribers")


@dataclass
class Subscriber:
    chat_id: int
    subscribed: bool
    plan: str = "free"
    created_at: str = ""
    updated_at: str = ""
    selected_asset: str | None = None


class SubscriberStore:
    """Load subscribers once and persist only state changes."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        file_path: str | Path = "data/subscribers.json",
        connect: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.database_url = (
            os.environ.get("DATABASE_URL", "") if database_url is None else database_url
        ).strip()
        self.file_path = Path(file_path)
        self._connect = connect or psycopg.connect
        self._sleep = sleep
        self._use_database = bool(self.database_url)
        self.subscribers: dict[int, Subscriber] = {}
        if not self.file_path.exists():
            self._write_file()
        self.load()

    @property
    def backend(self) -> str:
        return "postgres" if self._use_database else "json"

    def _with_retry(self, operation: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    self._sleep(0.25 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _connection(self):
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        return self._connect(self.database_url, connect_timeout=10)

    def load(self) -> dict[int, Subscriber]:
        if self._use_database:
            try:
                def fetch_rows():
                    with self._connection() as connection:
                        connection.execute(
                            """
                            CREATE TABLE IF NOT EXISTS subscribers (
                                chat_id BIGINT PRIMARY KEY,
                                subscribed BOOLEAN NOT NULL,
                                plan TEXT NOT NULL DEFAULT 'free',
                                selected_asset TEXT,
                                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                            )
                            """
                        )
                        connection.execute(
                            "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS "
                            "selected_asset TEXT"
                        )
                        cursor = connection.execute(
                            "SELECT chat_id, subscribed, plan, created_at, updated_at, "
                            "selected_asset "
                            "FROM subscribers"
                        )
                        return cursor.fetchall()

                rows = self._with_retry(fetch_rows)
                self.subscribers = {
                    int(row[0]): Subscriber(
                        chat_id=int(row[0]),
                        subscribed=bool(row[1]),
                        plan=str(row[2] or "free"),
                        created_at=self._timestamp(row[3]),
                        updated_at=self._timestamp(row[4]),
                        selected_asset=str(row[5]) if row[5] else None,
                    )
                    for row in rows
                }
                return self.subscribers
            except Exception as exc:
                logger.warning(
                    "Subscriber database unavailable (%s); using JSON fallback.",
                    type(exc).__name__,
                )
                self._use_database = False
        elif not self.database_url:
            logger.warning("DATABASE_URL is missing; using JSON subscriber storage.")
        return self._load_file()

    @staticmethod
    def _timestamp(value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        return str(value or "")

    def _load_file(self) -> dict[int, Subscriber]:
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
            rows = payload if isinstance(payload, list) else []
            self.subscribers = {
                int(row["chat_id"]): Subscriber(
                    chat_id=int(row["chat_id"]),
                    subscribed=bool(row["subscribed"]),
                    plan=str(row.get("plan", "free")),
                    created_at=str(row.get("created_at", "")),
                    updated_at=str(row.get("updated_at", "")),
                    selected_asset=(
                        str(row["selected_asset"])
                        if row.get("selected_asset")
                        else None
                    ),
                )
                for row in rows
                if isinstance(row, dict) and "chat_id" in row and "subscribed" in row
            }
        except FileNotFoundError:
            self.subscribers = {}
            self._write_file()
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Subscriber JSON file could not be read (%s); starting empty.",
                type(exc).__name__,
            )
            self.subscribers = {}
        return self.subscribers

    def set_subscribed(self, chat_id: int, subscribed: bool) -> bool:
        """Update one chat; return whether its subscription state changed."""
        chat_id = int(chat_id)
        current = self.subscribers.get(chat_id)
        subscribed = bool(subscribed)
        if current is not None and current.subscribed == subscribed:
            return False
        now = datetime.now(timezone.utc).isoformat()
        record = Subscriber(
            chat_id=chat_id,
            subscribed=subscribed,
            plan=current.plan if current else "free",
            created_at=current.created_at if current and current.created_at else now,
            updated_at=now,
            selected_asset=current.selected_asset if current else None,
        )
        self.subscribers[chat_id] = record
        if self._use_database:
            try:
                self._with_retry(lambda: self._upsert(record))
                return True
            except Exception as exc:
                logger.warning(
                    "Subscriber database write failed (%s); using JSON fallback.",
                    type(exc).__name__,
                )
                self._use_database = False
        self._write_file()
        return True

    def set_asset(self, chat_id: int, selected_asset: str) -> bool:
        """Select one signal asset for a chat and enable its subscription."""
        chat_id = int(chat_id)
        current = self.subscribers.get(chat_id)
        now = datetime.now(timezone.utc).isoformat()
        record = Subscriber(
            chat_id=chat_id,
            subscribed=True,
            plan=current.plan if current else "free",
            created_at=current.created_at if current and current.created_at else now,
            updated_at=now,
            selected_asset=selected_asset,
        )
        if (
            current is not None
            and current.subscribed
            and current.selected_asset == selected_asset
        ):
            return False
        self.subscribers[chat_id] = record
        if self._use_database:
            try:
                self._with_retry(lambda: self._upsert(record))
                return True
            except Exception as exc:
                logger.warning(
                    "Subscriber database write failed (%s); using JSON fallback.",
                    type(exc).__name__,
                )
                self._use_database = False
        self._write_file()
        return True

    def _upsert(self, record: Subscriber) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO subscribers
                    (chat_id, subscribed, plan, selected_asset, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (chat_id) DO UPDATE SET
                    subscribed = EXCLUDED.subscribed,
                    plan = EXCLUDED.plan,
                    selected_asset = EXCLUDED.selected_asset,
                    updated_at = NOW()
                """,
                (
                    record.chat_id,
                    record.subscribed,
                    record.plan,
                    record.selected_asset,
                    record.created_at,
                ),
            )

    def _write_file(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    [asdict(item) for item in self.subscribers.values()],
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.file_path)
        except OSError as exc:
            logger.warning(
                "Subscriber JSON fallback could not be written (%s).",
                type(exc).__name__,
            )

    def subscribed_chat_ids(self, selected_asset: str | None = None) -> list[int]:
        return sorted(
            chat_id
            for chat_id, subscriber in self.subscribers.items()
            if subscriber.subscribed
            and (
                selected_asset is None
                or subscriber.selected_asset == selected_asset
            )
        )

    def count(self) -> int:
        return sum(item.subscribed for item in self.subscribers.values())
