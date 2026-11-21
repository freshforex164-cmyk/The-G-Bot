"""One asyncio task per active user, each with its own broker connection. All errors are handled
inside ``run()``; nothing a worker raises can reach another user's worker."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

from ..clock import Clock, utc_day_start
from ..errors import (BrokerError, Disconnected, ModeMismatch, RateLimited, SessionExpired, VaultError)
from ..market.feed import SharedMarketFeed
from ..storage.repo import BotState, Repository
from ..vault.service import TokenVault
from .broker import BrokerClient, TradeOutcome, map_exception
from .models import DEMO, Decision, TradeRecord, TradingSettings
from .risk import RiskManager, RiskState
from .strategy import STRATEGIES, Strategy

log = logging.getLogger("autotrade.worker")

MAX_RECONNECTS = 8
MAX_UNEXPECTED = 3
MAX_PENDING_SIGNALS = 20
SEEN_SIGNAL_IDS = 500


class WorkerStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused_daily_limit"
    RECONNECTING = "reconnecting"
    DRAINING = "draining"
    STOPPED = "stopped"
    REAUTH_REQUIRED = "reauth_required"
    ERROR = "error"


def compute_profit(outcome: TradeOutcome, stake: float, payout: int | None) -> float | None:
    if outcome.result == "loss":
        return round(-stake, 2)
    if outcome.result == "draw":
        return 0.0
    if outcome.result == "win":
        if outcome.profit is not None and outcome.profit > 0:
            return round(outcome.profit, 2)
        return round(stake * (payout or 0) / 100, 2)
    return None


class UserWorker:
    def __init__(self, user_id: str, *, repo: Repository, vault: TokenVault, feed: SharedMarketFeed,
                 broker_factory: Callable[[str], BrokerClient], risk: RiskManager, clock: Clock,
                 is_halted: Callable[[], bool] = lambda: False,
                 strategies: dict[str, Callable[[], Strategy]] | None = None,
                 reconnect_base: float = 2.0):
        self.user_id = user_id
        self._repo, self._vault, self._feed = repo, vault, feed
        self._factory, self._risk, self._clock = broker_factory, risk, clock
        self._is_halted = is_halted
        self._strategies = strategies or STRATEGIES
        self._reconnect_base = reconnect_base
        self.settings = TradingSettings()
        self.status = WorkerStatus.STARTING
        self.last_error: str | None = None
        self.last_reject: str | None = None
        self.desired = True
        self._stop = asyncio.Event()
        self._drain_on_stop = True
        self._last_trade_at: float | None = None
        self._trackers: dict[str, asyncio.Task] = {}
        self._client_is_demo = True
        self._pending: deque[Decision] = deque(maxlen=MAX_PENDING_SIGNALS)
        self._seen: deque[str] = deque(maxlen=SEEN_SIGNAL_IDS)
        self._wake: asyncio.Event | None = None
        self.balance: float | None = None
        self.balance_kind: str | None = None
        self.balance_at: datetime | None = None
        self.signals_received = 0

    # -- control ------------------------------------------------------------------------------
    def request_stop(self, drain: bool = True) -> None:
        self.desired = False
        self._drain_on_stop = drain
        self._stop.set()

    def apply_settings(self, s: TradingSettings) -> None:
        self.settings = s

    @property
    def open_count(self) -> int:
        return len(self._trackers)

    def snapshot(self) -> dict:
        return {"status": self.status.value, "last_error": self.last_error, "last_skip_reason": self.last_reject,
                "open_trades": self.open_count, "follow_signals": self.settings.follow_signals,
                "signals_received": self.signals_received,
                "balance": self.balance, "balance_account": self.balance_kind,
                "balance_at": self.balance_at.isoformat() if self.balance_at else None}

    def submit_signal(self, d: Decision) -> str:
        """Queue a GBot signal for this user. Returns 'queued' or why it was ignored."""
        if not self.desired or self.status not in (WorkerStatus.RUNNING, WorkerStatus.PAUSED):
            return "not_running"
        if not self.settings.follow_signals:
            return "follow_signals_off"
        if d.signal_id and d.signal_id in self._seen:
            return "duplicate"
        if self._expired(d):
            return "expired"
        if d.signal_id:
            self._seen.append(d.signal_id)
        self.signals_received += 1
        self._pending.append(d)
        if self._wake is not None:
            self._wake.set()
        return "queued"

    def _expired(self, d: Decision) -> bool:
        if d.valid_until is None:
            return False
        until = d.valid_until if d.valid_until.tzinfo else d.valid_until.replace(tzinfo=timezone.utc)
        return self._clock.utcnow() >= until

    # -- main loop ----------------------------------------------------------------------------
    async def run(self) -> None:
        reconnects = unexpected = 0
        try:
            while not self._stop.is_set():
                self.settings = await self._repo.get_settings(self.user_id)
                s = self.settings
                kind = "demo" if s.mode == DEMO else "real"
                strategy = self._strategies[s.strategy]()
                broker: BrokerClient | None = None
                try:
                    self.status = WorkerStatus.STARTING
                    ssid = await self._vault.get(self.user_id, kind)
                    if ssid is None:
                        raise SessionExpired("no active token")
                    broker = self._factory(ssid)
                    await broker.connect()
                    self._client_is_demo = await broker.is_demo()
                    if self._client_is_demo != (s.mode == DEMO):
                        raise ModeMismatch("connected account type does not match the selected mode")
                    reconnects = unexpected = 0
                    self.status = WorkerStatus.RUNNING
                    await self._persist()
                    await self._session(broker, strategy, s)
                except SessionExpired as exc:
                    await self._vault.mark_expired(self.user_id, kind, str(exc))
                    await self._finish(WorkerStatus.REAUTH_REQUIRED, str(exc))
                    return
                except VaultError:
                    await self._vault.mark_expired(self.user_id, kind, "token cannot be decrypted")
                    await self._finish(WorkerStatus.REAUTH_REQUIRED, "token cannot be decrypted")
                    return
                except ModeMismatch as exc:
                    await self._finish(WorkerStatus.ERROR, str(exc))
                    return
                except (RateLimited, Disconnected, BrokerError) as exc:
                    reconnects += 1
                    self.last_error = type(exc).__name__
                    if reconnects > MAX_RECONNECTS:
                        await self._finish(WorkerStatus.ERROR, f"gave up after {MAX_RECONNECTS} reconnects")
                        return
                    self.status = WorkerStatus.RECONNECTING
                    await self._persist()
                    wait = max(getattr(exc, "retry_after", 0.0), min(self._reconnect_base * 2 ** (reconnects - 1), 60.0))
                    await self._sleep(wait)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    unexpected += 1
                    self.last_error = type(exc).__name__
                    log.exception("unexpected worker error for user %s", self.user_id)
                    if unexpected >= MAX_UNEXPECTED:
                        await self._finish(WorkerStatus.ERROR, f"unexpected error: {type(exc).__name__}")
                        return
                    await self._sleep(self._reconnect_base)
                finally:
                    for t in list(self._trackers.values()):
                        t.cancel()
                    await asyncio.gather(*self._trackers.values(), return_exceptions=True)
                    self._trackers.clear()
                    if broker is not None:
                        await asyncio.shield(broker.close())
            self.status = WorkerStatus.STOPPED
            await self._persist()
        except asyncio.CancelledError:
            self.status = WorkerStatus.STOPPED
            await self._persist()
            raise

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _finish(self, status: WorkerStatus, error: str) -> None:
        self.status, self.last_error, self.desired = status, error, False
        await self._persist()

    async def _persist(self) -> None:
        try:
            await self._repo.set_bot_state(BotState(
                self.user_id, self.desired, self.status.value, self.last_error, self._clock.utcnow()))
        except Exception:
            log.warning("could not persist bot state for user %s", self.user_id)

    # -- one connected session ----------------------------------------------------------------
    async def _session(self, broker: BrokerClient, strategy: Strategy, s0: TradingSettings) -> None:
        follow = s0.follow_signals
        symbols = () if follow else strategy.required_symbols(s0)
        await self._adopt_open_trades(broker, s0.mode)
        await self._refresh_balance(broker)
        if symbols:
            await self._feed.acquire(symbols)
            ev = self._feed.register()
        else:
            ev = asyncio.Event()      # woken by submit_signal()
        self._wake = ev
        if self._pending:
            ev.set()
        try:
            while not self._stop.is_set():
                s = self.settings
                if s.mode != s0.mode or s.strategy != s0.strategy or s.follow_signals != follow or \
                        (not follow and strategy.required_symbols(s) != symbols):
                    await self._drain(broker)
                    return  # run() reconnects with the new account / strategy
                try:
                    wake = getattr(strategy, "next_wake_seconds", None)
                    timeout = 1.0 if follow or not callable(wake) else wake()
                    await asyncio.wait_for(ev.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                ev.clear()
                if not await broker.is_alive():
                    raise Disconnected("connection is not receiving data")
                if follow:
                    while self._pending and not self._stop.is_set():
                        d = self._pending.popleft()
                        if self._expired(d):
                            self._note("signal_expired")
                            continue
                        await self._try_trade(broker, d, s, (), pushed=True)
                    continue
                ticks = {sym: self._feed.latest(sym) for sym in symbols}
                decision = strategy.on_tick(ticks, s)
                if decision is not None:
                    await self._try_trade(broker, decision, s, symbols)
            if self._drain_on_stop:
                await self._drain(broker)
        finally:
            self._wake = None
            if symbols:
                self._feed.unregister(ev)
                await self._feed.release(symbols)

    async def _refresh_balance(self, broker: BrokerClient) -> None:
        """Best effort: a balance read never stops trading."""
        fn = getattr(broker, "balance", None)
        if not callable(fn):
            return
        try:
            value = await asyncio.wait_for(fn(), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if value is not None:
            self.balance = round(float(value), 2)
            self.balance_kind = "demo" if self._client_is_demo else "real"
            self.balance_at = self._clock.utcnow()

    async def _drain(self, broker: BrokerClient) -> None:
        """Let open trades settle so their results are logged before the connection closes."""
        if not self._trackers:
            return
        self.status = WorkerStatus.DRAINING
        await self._persist()
        longest = max((self.settings.expiry_seconds, 60)) + 60
        await asyncio.wait(list(self._trackers.values()), timeout=longest)

    def _note(self, reason: str) -> None:
        if reason != self.last_reject:
            log.info("user %s: order skipped (%s)", self.user_id, reason)
        self.last_reject = reason

    async def _try_trade(self, broker: BrokerClient, d: Decision, s: TradingSettings, symbols: tuple[str, ...],
                         *, pushed: bool = False) -> None:
        now = self._clock.monotonic()
        pnl = await self._repo.realized_pnl_since(self.user_id, utc_day_start(self._clock.utcnow()), s.mode)
        # A pushed GBot signal was produced from a fresh closed candle; its freshness is its valid_until.
        ages = [0.0] if pushed else [self._feed.age(sym) for sym in symbols]
        state = RiskState(now=now, last_trade_at=self._last_trade_at, open_count=self.open_count, daily_pnl=pnl,
                          tick_ages=ages, client_is_demo=self._client_is_demo,
                          halted=self._is_halted())
        verdict = self._risk.check(s, d, state)
        if not verdict.ok:
            self._note(verdict.reason)
            self.status = WorkerStatus.PAUSED if verdict.halt else WorkerStatus.RUNNING
            return
        self.status = WorkerStatus.RUNNING
        try:
            payout = await broker.payout(d.asset)
        except BrokerError:
            payout = None
        pv = self._risk.check_payout(s, payout)
        if not pv.ok:
            self._note(pv.reason)
            return
        self.last_reject = None
        self._last_trade_at = now          # cooldown starts before the order, so a failure cannot cause rapid retries
        ref = uuid.uuid4().hex
        meta = {**d.meta, **({"signal_id": d.signal_id, "source": "gbot_signal"} if d.signal_id else {})}
        rec = TradeRecord(self.user_id, ref, s.mode, d.asset, d.direction, s.stake, d.expiry_seconds,
                          payout=payout, reason=d.reason, meta=meta, opened_at=self._clock.utcnow())
        await self._repo.insert_trade(rec)
        try:
            trade_id = await broker.place(d.asset, d.direction, s.stake, d.expiry_seconds)
        except BrokerError as exc:
            await self._repo.update_trade(self.user_id, ref, status="failed", error=str(exc)[:200])
            return
        except (Disconnected, RateLimited) as exc:
            # The broker may have accepted the order: never retry, flag for review.
            await self._repo.update_trade(self.user_id, ref, status="unknown", error=type(exc).__name__)
            raise
        except SessionExpired as exc:
            await self._repo.update_trade(self.user_id, ref, status="failed", error="session rejected")
            raise
        await self._repo.update_trade(self.user_id, ref, status="open", broker_trade_id=trade_id)
        self._spawn_tracker(broker, ref, trade_id, s.stake, payout, d.expiry_seconds + 45)

    # -- results ------------------------------------------------------------------------------
    def _spawn_tracker(self, broker: BrokerClient, ref: str, trade_id: str, stake: float,
                       payout: int | None, timeout: float) -> None:
        self._trackers[ref] = asyncio.create_task(self._track(broker, ref, trade_id, stake, payout, timeout))

    async def _track(self, broker, ref, trade_id, stake, payout, timeout) -> None:
        try:
            out = await broker.outcome(trade_id, timeout)
            profit = compute_profit(out, stake, payout)
            if profit is None:
                await self._repo.update_trade(self.user_id, ref, status="unknown", error="result unavailable")
            else:
                await self._repo.update_trade(self.user_id, ref, status="settled", outcome=out.result,
                                              profit=profit, settled_at=self._clock.utcnow())
                await self._refresh_balance(broker)
        except asyncio.CancelledError:
            raise  # left 'open'; the next session adopts it
        except Exception as exc:
            await self._repo.update_trade(self.user_id, ref, status="unknown", error=type(map_exception(exc)).__name__)
        finally:
            self._trackers.pop(ref, None)

    async def _adopt_open_trades(self, broker: BrokerClient, mode: str) -> None:
        for t in await self._repo.open_trades(self.user_id):
            if t.mode != mode or t.client_ref in self._trackers:
                continue
            if not t.broker_trade_id:   # crashed before the broker acknowledged it
                await self._repo.update_trade(self.user_id, t.client_ref, status="unknown", error="no broker acknowledgement")
                continue
            self._spawn_tracker(broker, t.client_ref, t.broker_trade_id, t.stake, t.payout, 90)
