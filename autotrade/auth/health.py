from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from ..clock import Clock
from ..errors import Disconnected, RateLimited, SessionExpired, VaultError
from ..storage.repo import Repository
from ..trading.broker import BrokerClient, account_is_demo, map_exception
from ..vault.service import TokenVault

log = logging.getLogger("autotrade.health")


@dataclass(frozen=True)
class HealthResult:
    kind: str
    ok: bool
    status: str   # active | expired | missing | unreachable
    reason: str = ""


class SessionHealth:
    """Checks that a stored SSID is still accepted by the broker. Only an explicit broker
    rejection marks a token expired; network trouble reports 'unreachable' and keeps the token."""

    def __init__(self, vault: TokenVault, repo: Repository, broker_factory: Callable[[str], BrokerClient],
                 clock: Clock | None = None, timeout: float = 40.0):
        self._vault, self._repo, self._factory = vault, repo, broker_factory
        self._clock = clock or Clock()
        self._timeout = timeout
        self._pool = None

    def attach_pool(self, pool) -> None:
        """Run checks through the shared balance pool: one socket per account, reused by balance reads
        and handed to the trading worker on start, instead of a new short-lived socket per check."""
        self._pool = pool

    async def check(self, user_id: str, kind: str, *, keep: bool = False) -> HealthResult:
        try:
            ssid = await self._vault.get(user_id, kind)
        except VaultError:
            await self._vault.mark_expired(user_id, kind, "token cannot be decrypted")
            return HealthResult(kind, False, "expired", "token cannot be decrypted")
        if ssid is None:
            row = await self._repo.get_credential(user_id, kind)
            return HealthResult(kind, False, "expired" if row else "missing", (row.last_error or "") if row else "")
        if self._pool is not None:
            return await self._check_pooled(user_id, kind, keep)
        broker = self._factory(ssid)
        try:
            await asyncio.wait_for(broker.connect(), timeout=self._timeout)
            if (await account_is_demo(broker, ssid)) != (kind == "demo"):
                raise SessionExpired("stored token does not match its account type")
        except SessionExpired as exc:
            await self._vault.mark_expired(user_id, kind, str(exc))
            return HealthResult(kind, False, "expired", str(exc))
        except (Disconnected, RateLimited, asyncio.TimeoutError) as exc:
            return HealthResult(kind, False, "unreachable", str(exc) or "timeout")
        except Exception as exc:
            mapped = map_exception(exc)
            if isinstance(mapped, SessionExpired):
                await self._vault.mark_expired(user_id, kind, str(mapped))
                return HealthResult(kind, False, "expired", str(mapped))
            return HealthResult(kind, False, "unreachable", type(mapped).__name__)
        finally:
            await broker.close()
        await self._vault.mark_valid(user_id, kind)
        return HealthResult(kind, True, "active")

    async def _check_pooled(self, user_id: str, kind: str, keep: bool) -> HealthResult:
        try:
            async with asyncio.timeout(self._timeout), self._pool.connection(user_id, kind):
                pass
        except SessionExpired as exc:          # the pool closed it, marked it expired and prompted /at_login
            await self._vault.mark_expired(user_id, kind, str(exc))
            return HealthResult(kind, False, "expired", str(exc))
        except (Disconnected, RateLimited, asyncio.TimeoutError) as exc:
            return HealthResult(kind, False, "unreachable", str(exc) or "timeout")
        except VaultError:
            await self._vault.mark_expired(user_id, kind, "token cannot be decrypted")
            return HealthResult(kind, False, "expired", "token cannot be decrypted")
        except Exception as exc:
            mapped = map_exception(exc)
            if isinstance(mapped, SessionExpired):
                await self._vault.mark_expired(user_id, kind, str(mapped))
                return HealthResult(kind, False, "expired", str(mapped))
            return HealthResult(kind, False, "unreachable", type(mapped).__name__)
        if not keep:
            await self._pool.discard(user_id, kind)     # background sweeps do not hold sockets open
        await self._vault.mark_valid(user_id, kind)
        return HealthResult(kind, True, "active")

    async def recheck_due(self, older_than_seconds: float, limit: int = 20) -> list[HealthResult]:
        cutoff = self._clock.utcnow() - timedelta(seconds=older_than_seconds)
        results = []
        for row in await self._repo.list_credentials_to_recheck(cutoff, limit):
            results.append(await self.check(row.user_id, row.kind))
        return results

    async def run_forever(self, interval: float, older_than_seconds: float, skip: Callable[[str], bool] = lambda u: False) -> None:
        """Background worker. ``skip(user_id)`` should return True for users with a running bot
        (their live connection already proves the token; a second connection could kick it)."""
        while True:
            try:
                cutoff = self._clock.utcnow() - timedelta(seconds=older_than_seconds)
                for row in await self._repo.list_credentials_to_recheck(cutoff, 20):
                    if skip(row.user_id):
                        continue
                    await self.check(row.user_id, row.kind)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health sweep failed")
            await asyncio.sleep(interval)
