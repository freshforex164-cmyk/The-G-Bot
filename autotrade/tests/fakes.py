from __future__ import annotations

import asyncio
import os
from collections import deque
from datetime import datetime, timedelta, timezone

from ..auth.ssid import account_kind, build_ssid, parse_ssid
from ..clock import Clock
from ..config import Config
from ..errors import Disconnected, SessionExpired
from ..runtime import build_runtime
from ..storage.repo import InMemoryRepository
from ..trading.broker import TradeOutcome
from ..trading.models import Decision, TradingSettings
from unittest.mock import patch
from ..vault.crypto import KeyRing
from ..notify import RecordingNotifier


class FakeClock(Clock):
    def __init__(self):
        self.t = 1000.0
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.t

    def utcnow(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.now += timedelta(seconds=seconds)


def ssid_for(name: str, kind: str = "demo") -> str:
    return build_ssid(f"sess-{name}", kind, uid=abs(hash(name)) % 1000 + 1)


class World:
    """Scripted broker behaviour for one session string."""

    def __init__(self):
        self.payout = 85
        self.reject = False
        self.connect_failures = 0
        self.place_exc: Exception | None = None
        self.results: deque[str] = deque()
        self.default_result = "win"
        self.placed: list[tuple] = []
        self.connects = 0
        self.closes = 0
        self.gate: asyncio.Event | None = None   # when set, results wait for it
        self.balance_exc: Exception | None = None  # raised by balance() (e.g. a session rejected mid-use)
        self.connect_exc: Exception | None = None  # raised by every connect() (e.g. HTTP 429)
        self.demo_report = None                    # None: from the SSID; a bool or an Exception: library misreport
        self.payouts: dict[str, int] = {}          # per-asset payout overrides


class Bus:
    """Tick queues shared by the (single) feed broker."""

    def __init__(self):
        self.queues: dict[str, asyncio.Queue] = {}
        self.streams = 0

    def q(self, symbol: str) -> asyncio.Queue:
        return self.queues.setdefault(symbol, asyncio.Queue())

    def push(self, symbol: str, price: float) -> None:
        self.q(symbol).put_nowait(price)


class FakeBroker:
    def __init__(self, ssid: str, worlds: dict[str, World], bus: Bus):
        self._ssid = ssid
        self._session = (parse_ssid(ssid) or {}).get("session", "")
        self.w = worlds.setdefault(self._session, World())
        self._bus = bus
        self._open = False

    async def connect(self):
        self.w.connects += 1
        if self.w.reject:
            raise SessionExpired("broker rejected the session")
        if self.w.connect_exc is not None:
            raise self.w.connect_exc
        if self.w.connect_failures > 0:
            self.w.connect_failures -= 1
            raise Disconnected("socket closed")
        self._open = True

    async def close(self):
        if self._open:
            self.w.closes += 1
        self._open = False

    async def is_demo(self):
        r = self.w.demo_report
        if isinstance(r, Exception):
            raise r
        return account_kind(self._ssid) == "demo" if r is None else r

    async def is_alive(self):
        return self._open

    async def payout(self, asset):
        return self.w.payouts.get(asset, self.w.payout)

    async def balance(self):
        if self.w.balance_exc is not None:
            raise self.w.balance_exc
        settled = sum(1 for _ in self.w.placed)
        return 10000.0 + settled   # changes after each trade so refreshes are observable

    async def place(self, asset, direction, amount, expiry):
        if self.w.place_exc:
            raise self.w.place_exc
        self.w.placed.append((asset, direction, amount, expiry))
        return f"{self._session}-{len(self.w.placed)}"

    async def outcome(self, trade_id, timeout):
        await asyncio.sleep(0.02)
        if self.w.gate is not None:
            await self.w.gate.wait()
        result = self.w.results.popleft() if self.w.results else self.w.default_result
        return TradeOutcome(result, None)

    async def ticks(self, symbol):
        self._bus.streams += 1
        q = self._bus.q(symbol)
        while True:
            yield await q.get()



SCRIPTED_GAP = 0.0015


class ScriptedDecision:
    """Test-only order input for risk/session tests, independent of production strategy rules."""
    name = "scripted"

    def required_symbols(self, s):
        return (s.symbol_real, s.symbol_otc)

    def on_tick(self, ticks, s):
        real, otc = ticks.get(s.symbol_real), ticks.get(s.symbol_otc)
        if real is None or otc is None or abs(real.price - otc.price) < SCRIPTED_GAP:
            return None
        return Decision(s.symbol_otc, "CALL" if real.price > otc.price else "PUT", 60, "scripted test decision")

class EngineModeRepository(InMemoryRepository):
    """Settings default to the bot's own engine (follow_signals=False) for engine/risk tests."""

    async def get_settings(self, user_id):
        s = self.settings.get(user_id)
        return s if s is not None else TradingSettings(follow_signals=False)

    async def save_settings(self, user_id, settings):
        self.settings[user_id] = settings.with_changes(follow_signals=False)


class Env:
    def __init__(self, *, allow_live: bool = False, engine_mode: bool = False):
        self.worlds: dict[str, World] = {}
        self.bus = Bus()
        self.clock = FakeClock()
        os.environ["AUTOTRADE_FEED_SSID"] = ssid_for("feed")
        self.cfg = Config(jwt_secret="x" * 40, allow_live=allow_live, handshake_timeout=5)
        self.repo = EngineModeRepository() if engine_mode else InMemoryRepository()
        self.alerts = RecordingNotifier()
        self.rt = build_runtime(
            self.cfg, repo=self.repo, keyring=KeyRing({1: os.urandom(32)}, 1),
            broker_factory=lambda ssid: FakeBroker(ssid, self.worlds, self.bus),
            backend_factory=lambda: None, clock=self.clock, reconnect_base=0.02, notifier=self.alerts)

    def world(self, name: str) -> World:
        return self.worlds.setdefault(f"sess-{name}", World())

    async def add_user(self, user: str, kinds=("demo",)):
        for k in kinds:
            await self.rt.vault.store(user, k, ssid_for(user, k))

    def tick_gap(self, gap: float, base: float = 1.08000):
        """Push a real/otc price pair with the given gap (real - otc)."""
        self.bus.push("EURUSD", base + gap)
        self.bus.push("EURUSD_otc", base)


async def wait_until(cond, timeout: float = 3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def wait_async(cond, timeout: float = 3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if await cond():
            return True
        await asyncio.sleep(0.01)
    return False
