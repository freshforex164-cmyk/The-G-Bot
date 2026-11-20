from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from typing import Any

from ..config import Config
from ..errors import SettingsError

DEMO = "demo"
LIVE = "live"
MODES = (DEMO, LIVE)


@dataclass(frozen=True)
class TradingSettings:
    """Per-user parameters. Defaults are conservative and Demo only."""

    mode: str = DEMO
    live_confirmed: bool = False       # set only by an explicit Live toggle, cleared on re-login
    strategy: str = "gap"
    symbol_real: str = "EURUSD"
    symbol_otc: str = "EURUSD_otc"
    gap_threshold: float = 0.0015
    stake: float = 1.0
    expiry_seconds: int = 60
    max_concurrent: int = 1
    daily_stop_loss: float = 10.0      # > 0 required: cannot be disabled
    daily_take_profit: float = 20.0    # > 0 required: cannot be disabled
    cooldown_seconds: int = 60
    min_payout: int = 80
    max_tick_age_seconds: float = 3.0

    def validate(self, cfg: Config) -> "TradingSettings":
        def bad(msg: str):
            raise SettingsError(msg)

        if self.mode not in MODES:
            bad("mode must be 'demo' or 'live'")
        if self.mode == LIVE and not self.live_confirmed:
            bad("live mode requires explicit confirmation")
        cap = cfg.live_max_stake if self.mode == LIVE else cfg.max_stake
        if not (1.0 <= self.stake <= cap):
            bad(f"stake must be between 1 and {cap:g} in {self.mode} mode")
        if not (15 <= self.expiry_seconds <= 900):
            bad("expiry_seconds must be between 15 and 900")
        if not (1 <= self.max_concurrent <= 5):
            bad("max_concurrent must be between 1 and 5")
        if not (self.daily_stop_loss > 0):
            bad("daily_stop_loss must be greater than 0")
        if not (self.daily_take_profit > 0):
            bad("daily_take_profit must be greater than 0")
        if self.stake > self.daily_stop_loss:
            bad("stake must not exceed daily_stop_loss")
        if not (5 <= self.cooldown_seconds <= 3600):
            bad("cooldown_seconds must be between 5 and 3600")
        if not (50 <= self.min_payout <= 100):
            bad("min_payout must be between 50 and 100")
        if not (0.5 <= self.max_tick_age_seconds <= 30):
            bad("max_tick_age_seconds must be between 0.5 and 30")
        if not (0 < self.gap_threshold <= 0.05):
            bad("gap_threshold must be between 0 and 0.05")
        for name in ("symbol_real", "symbol_otc"):
            v = getattr(self, name)
            if not (isinstance(v, str) and 3 <= len(v) <= 32 and v.replace("_", "").replace("-", "").replace("#", "").isalnum()):
                bad(f"{name} is not a valid symbol")
        if self.symbol_real == self.symbol_otc:
            bad("symbol_real and symbol_otc must differ")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradingSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def with_changes(self, **changes: Any) -> "TradingSettings":
        return replace(self, **changes)


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    received_at: float  # Clock.monotonic() when the tick arrived here (not the provider timestamp)


@dataclass(frozen=True)
class Decision:
    asset: str
    direction: str  # "CALL" | "PUT"
    expiry_seconds: int
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeRecord:
    user_id: str
    client_ref: str
    mode: str
    asset: str
    direction: str
    stake: float
    expiry_seconds: int
    status: str = "pending"      # pending | open | settled | failed | unknown
    broker_trade_id: str | None = None
    payout: int | None = None
    outcome: str | None = None   # win | loss | draw
    profit: float | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    opened_at: datetime | None = None
    settled_at: datetime | None = None
    error: str | None = None
