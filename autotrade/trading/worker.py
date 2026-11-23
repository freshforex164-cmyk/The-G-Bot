"""One asyncio task per active user, each with its own broker connection. All errors are handled
inside ``run()``; nothing a worker raises can reach another user's worker."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

from ..clock import Clock, utc_day_start
from ..errors import (BrokerError, Disconnected, ModeMismatch, RateLimited, SessionExpired, VaultError)
from ..market.feed import SharedMarketFeed
from ..notify import NullNotifier, Notifier, failed_text, low_balance_text, opened_text, settled_text, skipped_text
from ..storage.repo import BalanceRow, BotState, Repository
from ..vault.service import TokenVault
from .broker import BrokerClient, TradeOutcome, account_is_demo, map_exception
from .models import DEMO, Decision, TradeRecord, TradingSettings
from .risk import RiskManager, RiskState
from .router import route
from .strategy import STRATEGIES, MultiAssetStrategy, Strategy

log = logging.getLogger("autotrade.worker")

MAX_RECONNECTS = 8
MAX_UNEXPECTED = 3
MAX_PENDING_SIGNALS = 20
SEEN_SIGNAL_IDS = 500


class StakeTracker:
    """Stateful stake ladder for one user (in memory; a restart starts again at the base stake).

    fixed: always ``stake``. martingale: after a loss move one step up (stake x multiplier); after a win, or
    a loss on the last step, back to the base stake. A draw keeps the step. An unknown result (timeout) never
    escalates: it resets to the base stake, the lowest exposure. Settings changes and a new UTC day reset it."""

    def __init__(self) -> None:
        self.step = 0
        self.win_streak = 0
        self.loss_streak = 0
        self._key: tuple | None = None

    def sync(self, s: TradingSettings, day: object = None) -> None:
        key = (s.stake_mode, s.effective_base_stake, s.martingale_multiplier, s.martingale_max_steps, s.stake, day)
        if key != self._key:
            self._key, self.step = key, 0

    def stake_for(self, s: TradingSettings) -> float:
        return s.ladder_stake(self.step)

    def record(self, s: TradingSettings, outcome: str | None) -> None:
        if outcome == "win":
            self.win_streak, self.loss_streak, self.step = self.win_streak + 1, 0, 0
        elif outcome == "loss":
            self.win_streak, self.loss_streak = 0, self.loss_streak + 1
            if s.stake_mode == "martingale":
                self.step = 0 if self.step >= s.martingale_max_steps else self.step + 1
        elif outcome == "draw":
            pass
        else:
            self.step = 0
        if s.stake_mode != "martingale":
            self.step = 0


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
                 reconnect_base: float = 2.0, notifier: Notifier | None = None,
                 on_session_expired: Callable[[str, str, str], Awaitable[None]] | None = None,
                 adopt: Callable[[str], Awaitable[BrokerClient | None]] | None = None):
        self.user_id = user_id
        self._repo, self._vault, self._feed = repo, vault, feed
        self._factory, self._risk, self._clock = broker_factory, risk, clock
        self._is_halted = is_halted
        self._strategies = strategies or STRATEGIES
        self._reconnect_base = reconnect_base
        self._notifier = notifier or NullNotifier()
        self._on_session_expired = on_session_expired
        self._adopt = adopt          # first session reuses the pooled connection from the start-up check
        self._broker: BrokerClient | None = None
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
        self.stakes = StakeTracker()
        self.last_route: dict | None = None
        self._open_stakes: dict[str, float] = {}

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
                "balance_at": self.balance_at.isoformat() if self.balance_at else None,
                "last_route": self.last_route, "stake_step": self.stakes.step,
                "win_streak": self.stakes.win_streak}

    @property
    def follows_signals(self) -> bool:
        """On GBot's "who is auto-trading" list: running (or about to) and placing GBot signals."""
        return self.desired and self.settings.follow_signals and self.status in (
            WorkerStatus.STARTING, WorkerStatus.RUNNING, WorkerStatus.PAUSED, WorkerStatus.RECONNECTING)

    async def refresh_balance_now(self) -> float | None:
        """Fresh balance from the live connection (None when not connected right now)."""
        broker = self._broker
        if broker is None:
            return None
        return await self._refresh_balance(broker)

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
                factory = self._strategies[s.strategy]
                strategy = (MultiAssetStrategy(factory) if not s.follow_signals and s.route_assets
                            else factory())
                broker: BrokerClient | None = None
                try:
                    self.status = WorkerStatus.STARTING
                    ssid = await self._vault.get(self.user_id, kind)
                    if ssid is None:
                        raise SessionExpired("no active token")
                    adopt, self._adopt = self._adopt, None
                    if adopt is not None:
                        try:
                            broker = await adopt(kind)
                        except Exception:
                            broker = None
                    if broker is None:
                        broker = self._factory(ssid)
                        await broker.connect()
                    self._client_is_demo = await account_is_demo(broker, ssid)
                    if self._client_is_demo != (s.mode == DEMO):
                        raise ModeMismatch("connected account type does not match the selected mode")
                    reconnects = unexpected = 0
                    self.status = WorkerStatus.RUNNING
                    await self._persist()
                    await self._session(broker, strategy, s)
                except SessionExpired as exc:
                    await self._session_expired_now(kind, str(exc))
                    await self._finish(WorkerStatus.REAUTH_REQUIRED, str(exc))
                    return
                except VaultError:
                    await self._session_expired_now(kind, "token cannot be decrypted")
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

    async def _session_expired_now(self, kind: str, reason: str) -> None:
        """Mark the session expired and prompt /at_login (the manager de-duplicates the prompt)."""
        if self._on_session_expired is not None:
            try:
                await self._on_session_expired(self.user_id, kind, reason)
                return
            except Exception:
                log.info("re-login prompt failed for user %s", self.user_id)
        await self._vault.mark_expired(self.user_id, kind, reason)

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
        self._broker = broker
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
                    batch: list[Decision] = []
                    while self._pending and not self._stop.is_set():
                        d = self._pending.popleft()
                        if self._expired(d):
                            self._note("signal_expired")
                            self._alert_skip(d, "signal_expired")
                            continue
                        batch.append(d)
                    if batch:
                        await self._route_and_trade(broker, batch, s, pushed=True)
                    continue
                ticks = {sym: self._feed.latest(sym) for sym in symbols}
                many = getattr(strategy, "on_tick_all", None)
                if callable(many):
                    found = many(ticks, s)
                    if found:
                        await self._route_and_trade(broker, found, s)
                    continue
                decision = strategy.on_tick(ticks, s)
                if decision is not None:
                    await self._try_trade(broker, decision, s, symbols)
            if self._drain_on_stop:
                await self._drain(broker)
        finally:
            self._wake = None
            self._broker = None
            if symbols:
                self._feed.unregister(ev)
                await self._feed.release(symbols)

    async def _refresh_balance(self, broker: BrokerClient) -> float | None:
        """Best effort: a balance read never stops trading. Saved so it survives restarts."""
        fn = getattr(broker, "balance", None)
        if not callable(fn):
            return None
        try:
            value = await asyncio.wait_for(fn(), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if value is None or float(value) < 0:      # the library reports -1 when unknown
            return None
        self.balance = round(float(value), 2)
        self.balance_kind = "demo" if self._client_is_demo else "real"
        self.balance_at = self._clock.utcnow()
        try:
            await self._repo.save_balance(BalanceRow(self.user_id, self.balance_kind, self.balance, self.balance_at))
        except Exception:
            log.warning("could not persist balance for user %s", self.user_id)
        return self.balance

    # -- alerts (best effort) -----------------------------------------------------------------
    @staticmethod
    def _asset_name(d: Decision) -> str:
        return str(d.meta.get("asset_name") or d.asset)

    def _alert(self, text: str) -> None:
        try:
            self._notifier.send(self.user_id, text)
        except Exception:
            log.info("trade alert failed for user %s", self.user_id)

    def _alert_skip(self, d: Decision, reason: str, *, payout: int | None = None,
                    routed: tuple[Decision, int] | None = None) -> None:
        if d.signal_id:          # only GBot signals: the user would otherwise hear nothing about them
            self._alert(skipped_text(asset=self._asset_name(d), direction=d.direction, reason=reason,
                                     setup=d.reason or "", payout=payout,
                                     routed_to=self._asset_name(routed[0]) if routed else None,
                                     routed_payout=routed[1] if routed else None))

    # -- payout routing -----------------------------------------------------------------------
    async def _payouts(self, broker: BrokerClient, assets: list[str]) -> dict[str, int | None]:
        """Live payouts: the shared feed first (cached, no new socket), then this user's own connection."""
        wanted = list(dict.fromkeys(assets))
        got: dict[str, int | None] = {}
        reader = getattr(self._feed, "payouts", None)
        if callable(reader):
            try:
                got.update(await reader(wanted))
            except Exception:
                pass
        missing = [a for a in wanted if got.get(a) is None]
        batch = getattr(broker, "payouts", None)
        if missing and callable(batch) and len(missing) > 1:
            try:
                got.update(await asyncio.wait_for(batch(missing), timeout=6.0))
            except (BrokerError, asyncio.TimeoutError):
                pass
            missing = [a for a in wanted if got.get(a) is None]
        for a in missing:
            try:
                got[a] = await broker.payout(a)
            except BrokerError:
                got[a] = None
        return got

    async def _route_and_trade(self, broker: BrokerClient, decisions: list[Decision], s: TradingSettings,
                               *, pushed: bool = False) -> None:
        """Evaluate every candidate that has its own setup, drop those under the minimum payout and place
        the best ones first. A low-payout setup is never moved onto another asset."""
        payouts = await self._payouts(broker, [d.asset for d in decisions])
        res = route(decisions, payouts, s.min_payout)
        best = res.best
        self.last_route = {
            "candidates": [{"asset": d.asset, "payout": payouts.get(d.asset)} for d in decisions][:10],
            "chosen": best[0].asset if best else None, "payout": best[1] if best else None,
            "min_payout": s.min_payout}
        for d, reason, p in res.rejected:
            self._note(reason)
            self._alert_skip(d, reason, payout=p, routed=best if reason == "payout_below_minimum" else None)
        for d, p in res.ranked:
            if self._stop.is_set():
                break
            await self._try_trade(broker, d, s, (d.asset,) if not pushed else (), pushed=pushed, payout=p)

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
                         *, pushed: bool = False, payout: int | None = None) -> None:
        now = self._clock.monotonic()
        day = utc_day_start(self._clock.utcnow())
        pnl = await self._repo.realized_pnl_since(self.user_id, day, s.mode)
        self.stakes.sync(s, day)
        stake = self.stakes.stake_for(s)
        # A pushed GBot signal was produced from a fresh closed candle; its freshness is its valid_until.
        ages = [0.0] if pushed else [self._feed.age(sym) for sym in symbols]
        state = RiskState(now=now, last_trade_at=self._last_trade_at, open_count=self.open_count, daily_pnl=pnl,
                          tick_ages=ages, client_is_demo=self._client_is_demo,
                          halted=self._is_halted(), stake=stake,
                          open_exposure=sum(self._open_stakes.values()) if self._open_stakes else
                          float(s.stake) * self.open_count)
        verdict = self._risk.check(s, d, state)
        if not verdict.ok:
            self._note(verdict.reason)
            self._alert_skip(d, verdict.reason)
            self.status = WorkerStatus.PAUSED if verdict.halt else WorkerStatus.RUNNING
            return
        self.status = WorkerStatus.RUNNING
        if payout is None:            # the router already read it for routed candidates
            try:
                payout = await broker.payout(d.asset)
            except BrokerError:
                payout = None
        pv = self._risk.check_payout(s, payout)
        if not pv.ok:
            self._note(pv.reason)
            self._alert_skip(d, pv.reason, payout=payout)
            return
        # Demo and Real alike: only trade money that is there. A fresh reading; the last known one if the
        # broker does not answer; no reading at all -> skip (never trade blind).
        balance = await self._refresh_balance(broker)
        if balance is None:
            balance = self.balance
        if balance is None or balance < stake:
            reason = "balance_unavailable" if balance is None else "insufficient_balance"
            self._note(reason)
            if balance is None:
                self._alert_skip(d, reason)
            else:
                self._alert(low_balance_text(mode=s.mode, asset=self._asset_name(d), direction=d.direction,
                                             stake=stake, balance=balance, setup=d.reason or ""))
            return
        self.last_reject = None
        self._last_trade_at = now          # cooldown starts before the order, so a failure cannot cause rapid retries
        ref = uuid.uuid4().hex
        meta = {**d.meta, **({"signal_id": d.signal_id, "source": "gbot_signal"} if d.signal_id else {}),
                "stake_mode": s.stake_mode, "stake_step": self.stakes.step}
        name = self._asset_name(d)
        rec = TradeRecord(self.user_id, ref, s.mode, d.asset, d.direction, stake, d.expiry_seconds,
                          payout=payout, reason=d.reason, meta=meta, opened_at=self._clock.utcnow())
        await self._repo.insert_trade(rec)
        try:
            trade_id = await broker.place(d.asset, d.direction, stake, d.expiry_seconds)
        except BrokerError as exc:
            await self._repo.update_trade(self.user_id, ref, status="failed", error=str(exc)[:200])
            self._alert(failed_text(asset=name, direction=d.direction, setup=d.reason or ""))
            return
        except (Disconnected, RateLimited) as exc:
            # The broker may have accepted the order: never retry, flag for review.
            await self._repo.update_trade(self.user_id, ref, status="unknown", error=type(exc).__name__)
            raise
        except SessionExpired as exc:
            await self._repo.update_trade(self.user_id, ref, status="failed", error="session rejected")
            raise
        await self._repo.update_trade(self.user_id, ref, status="open", broker_trade_id=trade_id)
        setup = d.reason if d.reason and d.reason != "gbot_signal" else ""
        self._alert(opened_text(mode=s.mode, asset=name, direction=d.direction, setup=setup, stake=stake,
                                payout=payout, expiry_seconds=d.expiry_seconds))
        self._spawn_tracker(broker, ref, trade_id, stake, payout, d.expiry_seconds + 45,
                            info={"mode": s.mode, "asset": name, "direction": d.direction, "setup": setup})

    # -- results ------------------------------------------------------------------------------
    def _spawn_tracker(self, broker: BrokerClient, ref: str, trade_id: str, stake: float,
                       payout: int | None, timeout: float, info: dict | None = None) -> None:
        self._open_stakes[ref] = float(stake)
        self._trackers[ref] = asyncio.create_task(
            self._track(broker, ref, trade_id, stake, payout, timeout, info or {}))

    async def _track(self, broker, ref, trade_id, stake, payout, timeout, info) -> None:
        try:
            out = await broker.outcome(trade_id, timeout)
            profit = compute_profit(out, stake, payout)
            self.stakes.record(self.settings, out.result if profit is not None else "unknown")
            if profit is None:
                await self._repo.update_trade(self.user_id, ref, status="unknown", error="result unavailable")
            else:
                await self._repo.update_trade(self.user_id, ref, status="settled", outcome=out.result,
                                              profit=profit, settled_at=self._clock.utcnow())
                balance = await self._refresh_balance(broker)
                if info:
                    self._alert(settled_text(mode=info["mode"], asset=info["asset"], direction=info["direction"],
                                             stake=stake, outcome=out.result, profit=profit, balance=balance,
                                             setup=info.get("setup", "")))
        except asyncio.CancelledError:
            raise  # left 'open'; the next session adopts it
        except Exception as exc:
            self.stakes.record(self.settings, "unknown")
            await self._repo.update_trade(self.user_id, ref, status="unknown", error=type(map_exception(exc)).__name__)
        finally:
            self._trackers.pop(ref, None)
            self._open_stakes.pop(ref, None)

    async def _adopt_open_trades(self, broker: BrokerClient, mode: str) -> None:
        for t in await self._repo.open_trades(self.user_id):
            if t.mode != mode or t.client_ref in self._trackers:
                continue
            if not t.broker_trade_id:   # crashed before the broker acknowledged it
                await self._repo.update_trade(self.user_id, t.client_ref, status="unknown", error="no broker acknowledgement")
                continue
            self._spawn_tracker(broker, t.client_ref, t.broker_trade_id, t.stake, t.payout, 90,
                                info={"mode": t.mode, "asset": str(t.meta.get("asset_name") or t.asset),
                                      "direction": t.direction, "setup": t.reason or ""})
