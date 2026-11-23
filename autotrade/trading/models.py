from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from typing import Any

from ..config import Config
from ..errors import SettingsError

SETTINGS_DEFAULTS_VERSION = 2
# v1 -> v2: follow-mode max_concurrent 1 -> 2 and cooldown 60 s -> 5 s; the "gap" setting was removed.
_V1_DEFAULTS = {"max_concurrent": (1, 2), "cooldown_seconds": (60, 5)}

DEMO = "demo"
LIVE = "live"
MODES = (DEMO, LIVE)
FIXED = "fixed"
MARTINGALE = "martingale"
STAKE_MODES = (FIXED, MARTINGALE)


@dataclass(frozen=True)
class TradingSettings:
    """Per-user parameters. Defaults are conservative and Demo only."""

    mode: str = DEMO
    live_confirmed: bool = False       # set only by an explicit Live toggle, cleared on re-login
    strategy: str = "price_action"
    symbol_real: str = "EURUSD"
    symbol_otc: str = "EURUSD_otc"
    stake: float = 1.0
    expiry_seconds: int = 60
    max_concurrent: int = 2            # follow mode: two back-to-back signals can both be placed
    daily_stop_loss: float = 10.0      # > 0 required: cannot be disabled
    daily_take_profit: float = 20.0    # > 0 required: cannot be disabled
    cooldown_seconds: int = 5          # follow mode: a signal right after another one is not skipped
    min_payout: int = 80
    max_tick_age_seconds: float = 3.0
    # True: place exactly the GBot signals this user receives (no separate engine).
    # False: the service runs its own price-action engine on symbol_otc.
    follow_signals: bool = True
    # Stake strategy. "fixed": every trade uses ``stake``. "martingale": start at ``base_stake`` (or ``stake``
    # when unset), multiply by ``martingale_multiplier`` after each loss for at most ``martingale_max_steps``
    # steps, back to the base stake after a win or after the last step.
    stake_mode: str = FIXED
    martingale_multiplier: float = 2.0
    martingale_max_steps: int = 3
    base_stake: float | None = None
    # Engine mode only: extra OTC symbols scanned with their own candles (e.g. UKBrent_otc, XAUUSD_otc).
    # Each needs its own valid pattern; the payout router then picks the best one. Empty = symbol_otc only.
    route_assets: tuple[str, ...] = ()
    # Bumped when defaults change, so saved settings that still hold the old defaults get the new ones.
    defaults_version: int = SETTINGS_DEFAULTS_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.route_assets, list):
            object.__setattr__(self, "route_assets", tuple(self.route_assets))

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
        if self.expiry_seconds != 60:
            bad("expiry_seconds must be 60 (1 minute)")
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
        for name in ("symbol_real", "symbol_otc"):
            v = getattr(self, name)
            if not (isinstance(v, str) and 3 <= len(v) <= 32 and v.replace("_", "").replace("-", "").replace("#", "").isalnum()):
                bad(f"{name} is not a valid symbol")
        if not isinstance(self.follow_signals, bool):
            bad("follow_signals must be true or false")
        if self.symbol_real == self.symbol_otc:
            bad("symbol_real and symbol_otc must differ")
        if not isinstance(self.route_assets, tuple) or len(self.route_assets) > 4:
            bad("route_assets takes at most 4 symbols")
        for v in self.route_assets:
            if not (isinstance(v, str) and 3 <= len(v) <= 32 and v.replace("_", "").replace("-", "").replace("#", "").isalnum()):
                bad("route_assets contains an invalid symbol")
        if len(set(self.route_assets)) != len(self.route_assets) or self.symbol_otc in self.route_assets:
            bad("route_assets must be distinct and different from symbol_otc")
        if self.stake_mode not in STAKE_MODES:
            bad("stake_mode must be 'fixed' or 'martingale'")
        if not (isinstance(self.martingale_multiplier, (int, float)) and 1.1 <= self.martingale_multiplier <= 5.0):
            bad("martingale_multiplier must be between 1.1 and 5")
        if not (isinstance(self.martingale_max_steps, int) and 1 <= self.martingale_max_steps <= 6):
            bad("martingale_max_steps must be between 1 and 6")
        if self.base_stake is not None:
            if not (1.0 <= self.base_stake <= cap):
                bad(f"base_stake must be between 1 and {cap:g} in {self.mode} mode")
            if self.base_stake > self.daily_stop_loss:
                bad("base_stake must not exceed daily_stop_loss")
        if self.stake_mode == MARTINGALE and self.max_ladder_stake > cap:
            bad(f"the martingale ladder reaches {self.max_ladder_stake:g}, above the {cap:g} stake limit; "
                "lower base_stake, martingale_multiplier or martingale_max_steps")
        return self

    @property
    def effective_base_stake(self) -> float:
        return float(self.base_stake if self.base_stake is not None else self.stake)

    def ladder_stake(self, step: int) -> float:
        """Stake for a ladder step (0 = base). Fixed mode always returns ``stake``."""
        if self.stake_mode != MARTINGALE:
            return float(self.stake)
        step = max(0, min(int(step), self.martingale_max_steps))
        return round(self.effective_base_stake * self.martingale_multiplier ** step, 2)

    @property
    def max_ladder_stake(self) -> float:
        return self.ladder_stake(self.martingale_max_steps)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradingSettings":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}     # drops removed keys such as gap_threshold
        if int(values.get("defaults_version") or 1) < 2:
            for name, (old, new) in _V1_DEFAULTS.items():
                if values.get(name, old) == old:
                    values[name] = new
        if values.get("strategy") == "gap":                        # removed alias of price_action
            values["strategy"] = "price_action"
        values["defaults_version"] = SETTINGS_DEFAULTS_VERSION
        return cls(**values)

    def with_changes(self, **changes: Any) -> "TradingSettings":
        return replace(self, **changes)


@dataclass(frozen=True)
class Tick:
    symbol: str
    price: float
    received_at: float  # Clock.monotonic() when the tick arrived here (not the provider timestamp)
    broker_time: float | None = None


@dataclass(frozen=True)
class Decision:
    asset: str
    direction: str  # "CALL" | "PUT"
    expiry_seconds: int
    reason: str
    meta: dict[str, Any] = field(default_factory=dict)
    signal_id: str | None = None          # set for GBot signals pushed to the service
    valid_until: datetime | None = None   # pushed signals are never placed after this moment


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
