from __future__ import annotations

from dataclasses import dataclass, field

from .models import Decision, TradingSettings


@dataclass
class RiskState:
    now: float                        # Clock.monotonic()
    last_trade_at: float | None
    open_count: int
    daily_pnl: float                  # realized, current UTC day, current mode
    tick_ages: list[float] = field(default_factory=list)  # seconds, one per required symbol (inf if none)
    client_is_demo: bool = True
    halted: bool = False              # global kill switch


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""
    halt: bool = False                # daily limit reached: stop trading until the next UTC day


OK = Verdict(True)


class RiskManager:
    """Every order passes through here. Rules are evaluated in priority order and the first
    failing rule is returned so the reason can be shown to the user."""

    def check(self, s: TradingSettings, d: Decision, st: RiskState) -> Verdict:
        if st.halted:
            return Verdict(False, "trading_halted")
        if st.client_is_demo != (s.mode == "demo"):
            return Verdict(False, "account_mode_mismatch")
        if st.daily_pnl <= -s.daily_stop_loss:
            return Verdict(False, "daily_stop_loss", halt=True)
        if st.daily_pnl >= s.daily_take_profit:
            return Verdict(False, "daily_take_profit", halt=True)
        # Open trades could all lose: count their stake (plus this one) as exposure.
        if st.daily_pnl - s.stake * (st.open_count + 1) < -s.daily_stop_loss:
            return Verdict(False, "stop_loss_exposure")
        if st.open_count >= s.max_concurrent:
            return Verdict(False, "max_concurrent")
        if st.last_trade_at is not None and st.now - st.last_trade_at < s.cooldown_seconds:
            return Verdict(False, "cooldown")
        if not st.tick_ages or max(st.tick_ages) > s.max_tick_age_seconds:
            return Verdict(False, "stale_prices")
        return OK

    def check_payout(self, s: TradingSettings, payout: int | None) -> Verdict:
        if payout is None:
            return Verdict(False, "payout_unavailable")
        if payout < s.min_payout:
            return Verdict(False, "payout_below_minimum")
        return OK
