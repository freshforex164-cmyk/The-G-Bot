"""Payout-aware routing between assets that each have their own verified price-action setup.

The router never moves a setup to another chart: every candidate is a Decision produced from that asset's
own closed 1-minute candles (a GBot signal for its asset, or the engine's pattern on that symbol). It only
decides which of those candidates to place first and drops the ones whose live payout is below the user's
minimum. Order: highest payout first; on equal payout the stable high-yield assets (Brent Oil OTC, then
Gold OTC) come first; then arrival order."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Decision

# Pocket Option symbols: Brent Oil OTC, Gold OTC.
PRIORITY_ASSETS: tuple[str, ...] = ("UKBrent_otc", "XAUUSD_otc")


@dataclass(frozen=True)
class RouteResult:
    ranked: list[tuple[Decision, int]] = field(default_factory=list)          # eligible, best first
    rejected: list[tuple[Decision, str, int | None]] = field(default_factory=list)  # (decision, reason, payout)

    @property
    def best(self) -> tuple[Decision, int] | None:
        return self.ranked[0] if self.ranked else None


def has_verified_setup(d: Decision) -> bool:
    """A decision counts only when it carries a setup computed on its own asset."""
    if d.direction not in ("CALL", "PUT") or not d.asset or not str(d.reason or "").strip():
        return False
    origin = d.meta.get("setup_asset") if isinstance(d.meta, dict) else None
    return origin is None or origin == d.asset


def priority(asset: str) -> int:
    try:
        return PRIORITY_ASSETS.index(asset)
    except ValueError:
        return len(PRIORITY_ASSETS)


def route(decisions: list[Decision], payouts: dict[str, int | None], min_payout: int) -> RouteResult:
    eligible: list[tuple[int, int, int, Decision]] = []
    rejected: list[tuple[Decision, str, int | None]] = []
    seen: set[str] = set()
    for i, d in enumerate(decisions):
        if d.asset in seen:                       # one order per asset per evaluation
            rejected.append((d, "duplicate_asset", payouts.get(d.asset)))
            continue
        seen.add(d.asset)
        if not has_verified_setup(d):
            rejected.append((d, "no_verified_setup", payouts.get(d.asset)))
            continue
        p = payouts.get(d.asset)
        if p is None:
            rejected.append((d, "payout_unavailable", None))
        elif p < min_payout:
            rejected.append((d, "payout_below_minimum", p))
        else:
            eligible.append((-int(p), priority(d.asset), i, d))
    eligible.sort(key=lambda e: e[:3])
    return RouteResult([(d, -neg) for neg, _, _, d in eligible], rejected)
