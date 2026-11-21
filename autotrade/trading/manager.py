from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..auth.health import SessionHealth
from ..clock import Clock, utc_day_start
from ..config import Config
from ..errors import AutotradeError, ReauthRequired, SettingsError
from ..market.feed import SharedMarketFeed
from ..storage.repo import Repository
from ..vault.service import TokenVault
from .broker import BrokerClient
from .models import DEMO, LIVE, Decision, TradingSettings
from .risk import RiskManager
from .strategy import STRATEGIES
from .worker import UserWorker, WorkerStatus

log = logging.getLogger("autotrade.manager")


class TradingHalted(AutotradeError):
    pass


class CapacityError(AutotradeError):
    pass


class LiveDisabled(AutotradeError):
    pass


class ServiceUnavailable(AutotradeError):
    pass


_TERMINAL = {WorkerStatus.STOPPED, WorkerStatus.ERROR, WorkerStatus.REAUTH_REQUIRED}


class TradingManager:
    """Supervisor: owns the per-user workers, applies settings, enforces global switches."""

    def __init__(self, *, cfg: Config, repo: Repository, vault: TokenVault, health: SessionHealth,
                 feed: SharedMarketFeed, broker_factory: Callable[[str], BrokerClient],
                 risk: RiskManager | None = None, clock: Clock | None = None,
                 reconnect_base: float = 2.0):
        self._reconnect_base = reconnect_base
        self.cfg, self.repo, self.vault, self.health, self.feed = cfg, repo, vault, health, feed
        self._factory = broker_factory
        self._risk = risk or RiskManager()
        self._clock = clock or Clock()
        self.halted = cfg.trading_halted
        self._workers: dict[str, UserWorker] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def is_running(self, user_id: str) -> bool:
        t = self._tasks.get(user_id)
        return bool(t and not t.done())

    # -- start / stop -------------------------------------------------------------------------
    async def start(self, user_id: str) -> dict[str, Any]:
        async with self._lock(user_id):
            if self.is_running(user_id):
                return self._workers[user_id].snapshot()
            if self.halted:
                raise TradingHalted("trading is halted by the operator")
            if sum(1 for u in self._tasks if self.is_running(u)) >= self.cfg.max_workers:
                raise CapacityError("the service is at capacity, try again later")
            s = await self.repo.get_settings(user_id)
            if s.mode == LIVE and not (self.cfg.allow_live and s.live_confirmed):
                raise LiveDisabled("live trading is not enabled")
            s.validate(self.cfg)
            kind = "demo" if s.mode == DEMO else "real"
            res = await self.health.check(user_id, kind)
            if not res.ok:
                if res.status in ("missing", "expired"):
                    raise ReauthRequired(kind, res.status)
                raise ServiceUnavailable(f"broker unreachable: {res.reason}")
            worker = UserWorker(user_id, repo=self.repo, vault=self.vault, feed=self.feed,
                                broker_factory=self._factory, risk=self._risk, clock=self._clock,
                                is_halted=lambda: self.halted, reconnect_base=self._reconnect_base)
            worker.apply_settings(s)
            self._workers[user_id] = worker
            task = asyncio.create_task(worker.run(), name=f"worker:{user_id}")
            task.add_done_callback(lambda t, u=user_id: self._on_done(u, t))
            self._tasks[user_id] = task
            await worker._persist()
            await self.repo.audit(user_id, "bot_started", {"mode": s.mode})
            return worker.snapshot()

    def _on_done(self, user_id: str, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("worker for user %s ended with %s", user_id, type(task.exception()).__name__)

    async def stop(self, user_id: str, drain: bool = True) -> dict[str, Any]:
        async with self._lock(user_id):
            worker, task = self._workers.get(user_id), self._tasks.get(user_id)
            if worker is None or task is None or task.done():
                await self._mark_stopped(user_id)
                return {"status": WorkerStatus.STOPPED.value}
            worker.request_stop(drain=drain)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=180 if drain else 10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.repo.audit(user_id, "bot_stopped", {})
        return worker.snapshot()

    async def _mark_stopped(self, user_id: str) -> None:
        from ..storage.repo import BotState
        await self.repo.set_bot_state(BotState(user_id, False, "stopped", None, self._clock.utcnow()))

    # -- settings -----------------------------------------------------------------------------
    async def update_settings(self, user_id: str, patch: dict[str, Any], confirm_live: bool = False) -> TradingSettings:
        async with self._lock(user_id):
            cur = await self.repo.get_settings(user_id)
            changes = {k: v for k, v in patch.items() if v is not None}
            if "strategy" in changes and changes["strategy"] not in STRATEGIES:
                raise SettingsError("unknown strategy")
            if "mode" in changes and changes["mode"] != cur.mode:
                if changes["mode"] == LIVE:
                    if not self.cfg.allow_live:
                        raise LiveDisabled("live trading is not enabled on this service")
                    if not confirm_live:
                        raise SettingsError("switching to live requires confirm_live=true")
                    if (await self.vault.status(user_id))["real"]["status"] != "active":
                        raise ReauthRequired("real", "no active real-account session")
                    changes["live_confirmed"] = True
                else:
                    changes["live_confirmed"] = False
            elif "mode" in changes:
                changes.pop("mode")
            new = cur.with_changes(**changes).validate(self.cfg)
            await self.repo.save_settings(user_id, new)
            worker = self._workers.get(user_id)
            if worker and self.is_running(user_id):
                worker.apply_settings(new)
            await self.repo.audit(user_id, "settings_changed", {"fields": sorted(changes), "mode": new.mode})
            return new

    async def reset_to_demo(self, user_id: str, reason: str) -> None:
        """Called after a fresh login: Live must be re-confirmed explicitly."""
        cur = await self.repo.get_settings(user_id)
        if cur.mode == LIVE or cur.live_confirmed:
            new = cur.with_changes(mode=DEMO, live_confirmed=False)
            await self.repo.save_settings(user_id, new)
            worker = self._workers.get(user_id)
            if worker and self.is_running(user_id):
                worker.apply_settings(new)
            await self.repo.audit(user_id, "reset_to_demo", {"reason": reason})

    # -- GBot signals -------------------------------------------------------------------------
    async def submit_signal(self, user_id: str, decision: Decision) -> dict[str, Any]:
        """Hand a signal the user just received in Telegram to their running bot. Never raises for
        users without a running bot: most subscribers do not auto-trade."""
        worker = self._workers.get(user_id)
        if worker is None or not self.is_running(user_id):
            return {"accepted": False, "reason": "not_running"}
        if self.halted:
            return {"accepted": False, "reason": "trading_halted"}
        reason = worker.submit_signal(decision)
        return {"accepted": reason == "queued", "reason": reason}

    # -- reporting ----------------------------------------------------------------------------
    async def stats(self, user_id: str) -> dict[str, Any]:
        s = await self.repo.get_settings(user_id)
        now = self._clock.utcnow()
        day = utc_day_start(now)
        worker = self._workers.get(user_id)
        running = self.is_running(user_id)
        state = await self.repo.get_bot_state(user_id)
        bot = worker.snapshot() if worker and (running or worker.status in _TERMINAL) else \
            {"status": state.status if state else "stopped", "last_error": state.last_error if state else None,
             "open_trades": 0, "last_skip_reason": None}
        return {
            "bot": {**bot, "running": running, "halted_by_operator": self.halted},
            "mode": s.mode,
            "settings": s.to_dict(),
            "sessions": await self.vault.status(user_id),
            "today": {**(await self.repo.trade_stats(user_id, day)),
                      "realized_pnl": await self.repo.realized_pnl_since(user_id, day, s.mode),
                      "stop_loss": s.daily_stop_loss, "take_profit": s.daily_take_profit},
            "all_time": await self.repo.trade_stats(user_id, None),
            "recent_trades": [
                {"opened_at": t.opened_at.isoformat() if t.opened_at else None, "mode": t.mode, "asset": t.asset,
                 "direction": t.direction, "stake": t.stake, "status": t.status, "outcome": t.outcome,
                 "profit": t.profit, "payout": t.payout, "reason": t.reason}
                for t in await self.repo.recent_trades(user_id, 20)],
        }

    # -- lifecycle ----------------------------------------------------------------------------
    async def resume_all(self) -> int:
        """After a restart, resume bots that were running. Failures are per-user and isolated."""
        n = 0
        for user_id in await self.repo.list_desired_running():
            try:
                await self.start(user_id)
                n += 1
            except Exception as exc:
                log.warning("could not resume user %s: %s", user_id, type(exc).__name__)
                await self._mark_stopped(user_id)
        return n

    async def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self.feed.aclose()
