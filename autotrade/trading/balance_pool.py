"""Bounded balance-only sockets with per-account operation ownership.

Never used for orders. No SSIDs are retained here. Registry updates are short;
slow users cannot serialize all other balance checks or broker handshakes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..errors import Disconnected, RateLimited, SessionExpired, VaultError
from .broker import account_is_demo, map_exception


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
        self.cooldowns = {}
        self.lock = asyncio.Lock()  # registry only; never held during external I/O
        self._locks = {}
        self._lock_refs = {}
        self._slots = 0
        self._reaper = None

    @asynccontextmanager
    async def _account(self, key):
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_refs[key] = self._lock_refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._lock_refs[key] -= 1
            if not self._lock_refs[key]:
                self._lock_refs.pop(key, None)
                self._locks.pop(key, None)

    @staticmethod
    async def _close(broker):
        try:
            await asyncio.wait_for(broker.close(), timeout=5)
        except Exception:
            pass

    async def _drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            self._slots = max(0, self._slots - 1)
            await self._close(entry.broker)

    async def _reserve(self):
        evicted = None
        async with self.lock:
            if self._slots >= self.limit:
                idle = [k for k in self.entries if k not in self._locks or not self._locks[k].locked()]
                if not idle:
                    raise Disconnected('balance connection capacity is busy; try again shortly')
                key = min(idle, key=lambda k: self.entries[k].used_at)
                evicted = self.entries.pop(key)
                self._slots -= 1
            self._slots += 1
        try:
            if evicted is not None:
                await self._close(evicted.broker)
        except BaseException:
            self._slots = max(0, self._slots - 1)
            raise

    async def _report_expired(self, user_id, kind, reason):
        if self.on_expired is not None:
            try:
                await self.on_expired(user_id, kind, reason)
            except Exception:
                pass

    @staticmethod
    def _is_expiry(exc):
        return isinstance(exc, Exception) and isinstance(map_exception(exc), SessionExpired)

    def _note_rate_limit(self, key, exc):
        mapped = map_exception(exc) if isinstance(exc, Exception) else None
        if isinstance(mapped, RateLimited):
            self.cooldowns[key] = self.clock.monotonic() + max(30.0, float(mapped.retry_after or 0))

    async def _expire(self):
        now = self.clock.monotonic()
        for key, entry in list(self.entries.items()):
            if key in self._locks and self._locks[key].locked():
                continue
            async with self._account(key):
                if self.entries.get(key) is entry and now - entry.used_at >= self.idle_seconds:
                    await self._drop(key)
        for key, until in list(self.cooldowns.items()):
            if until <= now:
                self.cooldowns.pop(key, None)

    async def _reap(self):
        while True:
            await asyncio.sleep(self.idle_seconds)
            await self._expire()

    @asynccontextmanager
    async def connection(self, user_id, kind):
        key = (user_id, kind)
        async with asyncio.timeout(40), self._account(key):
            if self._reaper is None:
                self._reaper = asyncio.create_task(self._reap(), name='balance-pool-cleanup')
            until = self.cooldowns.get(key)
            if until is not None and self.clock.monotonic() < until:
                raise RateLimited('balance checks are paused after a rate limit', retry_after=until - self.clock.monotonic())
            row = await self.repo.get_credential(user_id, kind)
            entry = self.entries.get(key)
            if entry and (row is None or row.status != 'active' or row.ciphertext != entry.ciphertext
                          or self.clock.monotonic() - entry.used_at >= self.idle_seconds):
                await self._drop(key)
                entry = None
            if row is None or row.status != 'active':
                raise SessionExpired('no active session')
            if entry and not await entry.broker.is_alive():
                await self._drop(key)
                entry = None
            if entry is None:
                ssid = await self.vault.get(user_id, kind)
                if ssid is None:
                    raise VaultError('session unavailable')
                await self._reserve()
                broker = None
                try:
                    broker = self.factory(ssid)
                    await asyncio.wait_for(broker.connect(), timeout=25)
                    if await account_is_demo(broker, ssid) != (kind == 'demo'):
                        raise SessionExpired('stored session account mismatch')
                except BaseException as exc:
                    self._slots = max(0, self._slots - 1)
                    if broker is not None:
                        await self._close(broker)
                    self._note_rate_limit(key, exc)
                    if self._is_expiry(exc):
                        await self._report_expired(user_id, kind, 'rejected by the broker')
                    raise
                entry = Entry(broker, row.ciphertext, self.clock.monotonic())
                self.entries[key] = entry
            try:
                yield entry.broker
            except BaseException as exc:
                await self._drop(key)
                self._note_rate_limit(key, exc)
                if self._is_expiry(exc):
                    await self._report_expired(user_id, kind, 'rejected during balance check')
                raise
            finally:
                entry.used_at = self.clock.monotonic()

    async def take(self, user_id, kind):
        """Transfer ownership only after validation succeeds; failure leaves it pooled."""
        key = (user_id, kind)
        async with asyncio.timeout(40), self._account(key):
            entry = self.entries.get(key)
            if entry is None:
                return None
            row = await self.repo.get_credential(user_id, kind)
            try:
                alive = await entry.broker.is_alive()
            except Exception:
                alive = False
            if row is None or row.status != 'active' or row.ciphertext != entry.ciphertext or not alive:
                await self._drop(key)
                return None
            self.entries.pop(key)
            self._slots = max(0, self._slots - 1)
            return entry.broker

    async def discard(self, user_id, kind):
        async with self._account((user_id, kind)):
            await self._drop((user_id, kind))

    async def close(self):
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        for key in list(self.entries):
            async with self._account(key):
                await self._drop(key)
