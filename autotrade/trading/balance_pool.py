"""Bounded, serialized balance-only connections keyed by user and account.

Never used for orders. Connections expire after 60 seconds of inactivity; a
changed ciphertext invalidates only its account slot. No SSIDs are retained here.
A session the broker rejects is torn down at once and reported through ``on_expired``
(the manager marks it expired and sends the user an /at_login prompt).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..errors import Disconnected, SessionExpired, VaultError
from .broker import map_exception


@dataclass
class Entry:
    broker: object
    ciphertext: bytes
    used_at: float


class BalancePool:
    def __init__(self, repo, vault, factory, clock, *, limit=16, idle_seconds=60, on_expired=None):
        self.repo, self.vault, self.factory, self.clock = repo, vault, factory, clock
        self.on_expired = on_expired
        self.limit, self.idle_seconds = max(1, limit), idle_seconds
        self.entries = {}
        self.lock = asyncio.Lock()
        self._reaper = None

    async def _drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            try:
                await asyncio.wait_for(entry.broker.close(), timeout=5)
            except Exception:
                pass

    async def _report_expired(self, user_id, kind, reason):
        if self.on_expired is None:
            return
        try:
            await self.on_expired(user_id, kind, reason)
        except Exception:
            pass

    @staticmethod
    def _is_expiry(exc):
        return isinstance(exc, Exception) and isinstance(map_exception(exc), SessionExpired)

    async def _expire(self):
        now = self.clock.monotonic()
        for key, entry in list(self.entries.items()):
            if now - entry.used_at >= self.idle_seconds:
                await self._drop(key)

    async def _reap(self):
        while True:
            await asyncio.sleep(self.idle_seconds)
            async with self.lock:
                await self._expire()

    @asynccontextmanager
    async def connection(self, user_id, kind):
        # Balance requests are rare and short; one global lock also bounds concurrent
        # handshakes and guarantees an entry cannot be evicted while in use.
        async with self.lock:
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap(), name="balance-pool-cleanup")
            await self._expire()
            key = (user_id, kind)
            row = await self.repo.get_credential(user_id, kind)
            entry = self.entries.get(key)
            if entry and (row is None or row.status != "active" or row.ciphertext != entry.ciphertext):
                await self._drop(key)
                entry = None
            if row is None or row.status != "active":
                raise SessionExpired("no active session")
            if entry and not await entry.broker.is_alive():
                await self._drop(key)
                entry = None
            if entry is None:
                ssid = await self.vault.get(user_id, kind)
                if ssid is None:
                    raise VaultError("session unavailable")
                if len(self.entries) >= self.limit:
                    await self._drop(min(self.entries, key=lambda k: self.entries[k].used_at))
                broker = self.factory(ssid)
                try:
                    await asyncio.wait_for(broker.connect(), timeout=25)
                    if await broker.is_demo() != (kind == "demo"):
                        raise SessionExpired("stored session account mismatch")
                except BaseException as exc:
                    try:
                        await asyncio.wait_for(broker.close(), timeout=5)
                    except Exception:
                        pass
                    if self._is_expiry(exc):
                        await self._report_expired(user_id, kind, "rejected by the broker")
                    raise
                entry = Entry(broker, row.ciphertext, self.clock.monotonic())
                self.entries[key] = entry
            try:
                yield entry.broker
            except BaseException as exc:
                await self._drop(key)          # clean teardown: the socket is closed before anything else
                if self._is_expiry(exc):
                    await self._report_expired(user_id, kind, "rejected during balance check")
                raise
            finally:
                entry.used_at = self.clock.monotonic()

    async def discard(self, user_id, kind):
        async with self.lock:
            await self._drop((user_id, kind))

    async def close(self):
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        async with self.lock:
            for key in list(self.entries):
                await self._drop(key)