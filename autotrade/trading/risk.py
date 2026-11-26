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
    stake: float | None = None        # stake of this order (Martingale ladder); None = settings stake
    open_exposure: float | None = None  # sum of open trades' stakes; None = settings stake x open trades


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
        # Daily limits are optional (user-set); None = no limit.
        if s.daily_stop_loss is not None and st.daily_pnl <= -s.daily_stop_loss:
            return Verdict(False, "daily_stop_loss", halt=True)
        if s.daily_take_profit is not None and st.daily_pnl >= s.daily_take_profit:
            return Verdict(False, "daily_take_profit", halt=True)
        # No 'open exposure' pre-check: it blocked valid signals ("stop-loss limit") while trades were open.
        if st.open_count >= s.max_concurrent:
            return Verdict(False, "max_concurrent")
        if st.last_trade_at is not None and st.now - st.last_trade_at < s.cooldown_seconds:
            return Verdict(False, "cooldown")
        if not st.tick_ages or max(st.tick_ages) > s.max_tick_age_seconds:
            return Verdict(False, "stale_prices")
        return OK

    def check_balance(self, s: TradingSettings, balance: float | None) -> Verdict:
        """Optional Stop Balance: pause once the balance is at or below the user's floor."""
        if s.stop_balance is not None and balance is not None and balance <= s.stop_balance:
            return Verdict(False, "stop_balance", halt=True)
        return OK

    def check_payout(self, s: TradingSettings, payout: int | None) -> Verdict:
        if payout is None:
            return Verdict(False, "payout_unavailable")
        if payout < s.min_payout:
            return Verdict(False, "payout_below_minimum")
        return OK
