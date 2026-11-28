from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..trading.models import TradeRecord, TradingSettings
from .repo import BalanceRow, BotState, CredentialRow
from .transient import is_transient

log = logging.getLogger("autotrade.postgres")

# Always applied at start-up (idempotent), even without AUTOTRADE_APPLY_SCHEMA, so tables added after the
# first deploy exist on databases that were set up earlier.
MIGRATIONS = """
create table if not exists autotrade_balances (
  user_id    text          not null,
  kind       text          not null check (kind in ('demo','real')),
  balance    numeric(14,2) not null,
  updated_at timestamptz   not null default now(),
  primary key (user_id, kind)
);
alter table autotrade_balances enable row level security;
"""

_TRADE_COLUMNS = {"status", "broker_trade_id", "outcome", "profit", "settled_at", "error", "opened_at"}
_TRADE_SELECT = ("user_id, client_ref, mode, asset, direction, stake, expiry_seconds, status, broker_trade_id, "
                 "payout, outcome, profit, reason, meta, opened_at, settled_at, error")


def _trade(r) -> TradeRecord:
    return TradeRecord(user_id=r[0], client_ref=r[1], mode=r[2], asset=r[3], direction=r[4], stake=float(r[5]),
                       expiry_seconds=r[6], status=r[7], broker_trade_id=r[8], payout=r[9], outcome=r[10],
                       profit=float(r[11]) if r[11] is not None else None, reason=r[12], meta=r[13] or {},
                       opened_at=r[14], settled_at=r[15], error=r[16])


class PostgresRepository:
    """psycopg 3 async pool implementation of ``Repository``."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10):
        from psycopg_pool import AsyncConnectionPool
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        # Preserve explicit DSN/SSL options; only supply missing liveness defaults.
        options = conninfo_to_dict(dsn)
        for key, value in {"connect_timeout": "10", "keepalives": "1", "keepalives_idle": "30",
                           "keepalives_interval": "10", "keepalives_count": "3"}.items():
            options.setdefault(key, value)
        self._sql_timeouts = tuple(os.getenv(name, default).strip() for name, default in (
            ("AUTOTRADE_DB_STATEMENT_TIMEOUT_MS", "15000"),
            ("AUTOTRADE_DB_LOCK_TIMEOUT_MS", "5000"),
            ("AUTOTRADE_DB_IDLE_TRANSACTION_TIMEOUT_MS", "30000")))
        if any(not value.isdigit() for value in self._sql_timeouts):
            raise ValueError("database timeout settings must be non-negative integer milliseconds")
        self._pool = AsyncConnectionPool(
            make_conninfo(**options), min_size=min_size, max_size=max_size, open=False,
            check=AsyncConnectionPool.check_connection, timeout=10, max_waiting=32,
            max_idle=60, max_lifetime=900, reconnect_timeout=60,
            reconnect_failed=self._reconnect_failed)

    @staticmethod
    async def _reconnect_failed(pool) -> None:
        # Do not log conninfo: it contains the database password.
        log.warning("database reconnect window exhausted; next checkout will retry")

    async def open(self, *, apply_schema: bool = False) -> None:
        await self._pool.open()
        if apply_schema:
            sql = (Path(__file__).resolve().parent.parent / "schema.sql").read_text()
            async with self._pool.connection() as conn:
                await conn.execute(sql)
        try:
            async with self._pool.connection() as conn:
                await conn.execute(MIGRATIONS)
        except Exception as exc:     # e.g. no CREATE privilege: balances just are not persisted
            import logging
            logging.getLogger("autotrade.postgres").warning("start-up migration failed: %s", type(exc).__name__)

    async def close(self) -> None:
        await self._pool.close()

    async def _set_timeouts(self, conn) -> None:
        # Transaction-local settings work with Neon transaction pooling. Preserve
        # nonzero database/DSN operator settings; never blindly replay writes.
        await conn.execute("""select
            set_config('statement_timeout', case when current_setting('statement_timeout') = '0'
                then %s else current_setting('statement_timeout') end, true),
            set_config('lock_timeout', case when current_setting('lock_timeout') = '0'
                then %s else current_setting('lock_timeout') end, true),
            set_config('idle_in_transaction_session_timeout',
                case when current_setting('idle_in_transaction_session_timeout') = '0'
                then %s else current_setting('idle_in_transaction_session_timeout') end, true)
            """, self._sql_timeouts)

    async def _exec(self, sql: str, params: tuple = ()) -> None:
        # Never replay writes after an SSL drop: COMMIT might already have succeeded.
        async with self._pool.connection() as conn:
            await self._set_timeouts(conn)
            await conn.execute(sql, params)

    async def _all(self, sql: str, params: tuple = ()) -> list[tuple]:
        # SELECT-only callers can safely retry with a new checked-out transaction.
        for attempt in range(3):
            try:
                async with self._pool.connection() as conn:
                    await self._set_timeouts(conn)
                    cur = await conn.execute(sql, params)
                    return await cur.fetchall()
            except Exception as exc:
                if not is_transient(exc) or attempt == 2:
                    raise
                log.warning("database read interrupted (%s); retry %d/2", type(exc).__name__, attempt + 1)
                await asyncio.sleep(0.1 * 2 ** attempt)
        raise RuntimeError("unreachable read retry state")

    # credentials -----------------------------------------------------------------------------
    async def upsert_credential(self, row: CredentialRow) -> None:
        await self._exec(
            """insert into autotrade_credentials (user_id, kind, ciphertext, key_version, status, captured_at,
                   last_checked_at, last_error, meta)
               values (%s,%s,%s,%s,%s,coalesce(%s, now()),%s,%s,%s)
               on conflict (user_id, kind) do update set ciphertext=excluded.ciphertext,
                   key_version=excluded.key_version, status=excluded.status, captured_at=excluded.captured_at,
                   last_checked_at=excluded.last_checked_at, last_error=excluded.last_error, meta=excluded.meta""",
            (row.user_id, row.kind, row.ciphertext, row.key_version, row.status, row.captured_at,
             row.last_checked_at, row.last_error, json.dumps(row.meta)))

    @staticmethod
    def _cred(r) -> CredentialRow:
        return CredentialRow(r[0], r[1], bytes(r[2]), r[3], r[4], r[5], r[6], r[7], r[8] or {})

    _CRED = "user_id, kind, ciphertext, key_version, status, captured_at, last_checked_at, last_error, meta"

    async def get_credential(self, user_id, kind):
        rows = await self._all(f"select {self._CRED} from autotrade_credentials where user_id=%s and kind=%s", (user_id, kind))
        return self._cred(rows[0]) if rows else None

    async def list_credentials(self, user_id):
        return [self._cred(r) for r in await self._all(f"select {self._CRED} from autotrade_credentials where user_id=%s", (user_id,))]

    async def mark_credential(self, user_id, kind, status, error, now) -> None:
        await self._exec("update autotrade_credentials set status=%s, last_error=%s, last_checked_at=%s where user_id=%s and kind=%s",
                         (status, error, now, user_id, kind))

    async def delete_credentials(self, user_id) -> None:
        await self._exec("delete from autotrade_credentials where user_id=%s", (user_id,))

    async def delete_credential(self, user_id, kind) -> None:
        await self._exec("delete from autotrade_credentials where user_id=%s and kind=%s", (user_id, kind))

    async def list_credentials_to_recheck(self, older_than, limit):
        rows = await self._all(
            f"select {self._CRED} from autotrade_credentials where status='active' "
            "and (last_checked_at is null or last_checked_at < %s) order by last_checked_at nulls first limit %s",
            (older_than, limit))
        return [self._cred(r) for r in rows]

    # settings --------------------------------------------------------------------------------
    async def get_settings(self, user_id) -> TradingSettings:
        rows = await self._all("select settings from autotrade_settings where user_id=%s", (user_id,))
        return TradingSettings.from_dict(rows[0][0]) if rows else TradingSettings()

    async def save_settings(self, user_id, settings: TradingSettings) -> None:
        await self._exec(
            """insert into autotrade_settings (user_id, settings) values (%s,%s)
               on conflict (user_id) do update set settings=excluded.settings, updated_at=now()""",
            (user_id, json.dumps(settings.to_dict())))

    # trades ----------------------------------------------------------------------------------
    async def insert_trade(self, t: TradeRecord) -> None:
        await self._exec(
            """insert into autotrade_trades (user_id, client_ref, mode, asset, direction, stake, expiry_seconds, status,
                   broker_trade_id, payout, reason, meta, opened_at)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,coalesce(%s, now()))""",
            (t.user_id, t.client_ref, t.mode, t.asset, t.direction, t.stake, t.expiry_seconds, t.status,
             t.broker_trade_id, t.payout, t.reason, json.dumps(t.meta), t.opened_at))

    async def update_trade(self, user_id, client_ref, **changes: Any) -> None:
        cols = [c for c in changes if c in _TRADE_COLUMNS]
        if not cols:
            return
        sets = ", ".join(f"{c}=%s" for c in cols)
        await self._exec(f"update autotrade_trades set {sets} where user_id=%s and client_ref=%s",
                         tuple(changes[c] for c in cols) + (user_id, client_ref))

    async def open_trades(self, user_id):
        return [_trade(r) for r in await self._all(
            f"select {_TRADE_SELECT} from autotrade_trades where user_id=%s and status in ('pending','open')", (user_id,))]

    async def realized_pnl_since(self, user_id, since, mode) -> float:
        rows = await self._all(
            "select coalesce(sum(profit),0) from autotrade_trades "
            "where user_id=%s and mode=%s and status='settled' and settled_at >= %s", (user_id, mode, since))
        return round(float(rows[0][0]), 2)

    async def recent_trades(self, user_id, limit, mode=None):
        return [_trade(r) for r in await self._all(
            f"select {_TRADE_SELECT} from autotrade_trades where user_id=%s and (%s::text is null or mode=%s) order by opened_at desc limit %s", (user_id, mode, mode, limit))]

    async def trade_stats(self, user_id, since, mode=None):
        r = (await self._all(
            """select count(*),
                      count(*) filter (where status='settled'),
                      count(*) filter (where status in ('pending','open')),
                      count(*) filter (where status in ('failed','unknown')),
                      count(*) filter (where outcome='win'),
                      count(*) filter (where outcome='loss'),
                      count(*) filter (where outcome='draw'),
                      coalesce(sum(profit) filter (where status='settled'),0)
               from autotrade_trades where user_id=%s and (%s::text is null or mode=%s) and (%s::timestamptz is null or opened_at >= %s)""",
            (user_id, mode, mode, since, since)))[0]
        decided = r[4] + r[5]
        return {"trades": r[0], "settled": r[1], "open": r[2], "failed": r[3], "wins": r[4], "losses": r[5],
                "draws": r[6], "win_rate": round(r[4] / decided * 100, 1) if decided else None,
                "net_profit": round(float(r[7]), 2)}

    # bot state / audit -----------------------------------------------------------------------
    async def set_bot_state(self, s: BotState) -> None:
        await self._exec(
            """insert into autotrade_bot_state (user_id, desired_running, status, last_error, updated_at)
               values (%s,%s,%s,%s,coalesce(%s, now()))
               on conflict (user_id) do update set desired_running=excluded.desired_running, status=excluded.status,
                   last_error=excluded.last_error, updated_at=excluded.updated_at""",
            (s.user_id, s.desired_running, s.status, s.last_error, s.updated_at))

    async def get_bot_state(self, user_id):
        rows = await self._all("select user_id, desired_running, status, last_error, updated_at from autotrade_bot_state where user_id=%s", (user_id,))
        return BotState(*rows[0]) if rows else None

    async def list_desired_running(self):
        return [r[0] for r in await self._all("select user_id from autotrade_bot_state where desired_running")]

    # balances ----------------------------------------------------------------------------------
    async def save_balance(self, row: BalanceRow) -> None:
        await self._exec(
            """insert into autotrade_balances (user_id, kind, balance, updated_at) values (%s,%s,%s,coalesce(%s, now()))
               on conflict (user_id, kind) do update set balance=excluded.balance, updated_at=excluded.updated_at""",
            (row.user_id, row.kind, row.balance, row.updated_at))

    async def get_balances(self, user_id) -> dict[str, BalanceRow]:
        rows = await self._all("select user_id, kind, balance, updated_at from autotrade_balances where user_id=%s",
                               (user_id,))
        return {r[1]: BalanceRow(r[0], r[1], float(r[2]), r[3]) for r in rows}

    async def audit(self, user_id, event, detail) -> None:
        await self._exec("insert into autotrade_audit (user_id, event, detail) values (%s,%s,%s)",
                         (user_id, event, json.dumps(detail)))
