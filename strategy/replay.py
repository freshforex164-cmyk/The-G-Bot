"""Single-configuration, approximate 1-minute price-action replay."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import config
from broker.pocket_option import CandleHistoryUnavailable, PocketOptionBroker
from data.candles import Candle, CandleBuilder, CandleDataError, _normalize_broker_timestamp
from market_assets import CATALOG, MarketAsset, match_runtime_assets
from strategy.model import Signal
from strategy.price_action import PriceActionStrategy, SETUP_LABELS
from strategy.settings import STRATEGY_SETTINGS


@dataclass(frozen=True)
class ReplayOutcome:
    signal: Signal
    expiry_price: float
    result: str
    outcome_type: str = "PROXY"


@dataclass
class ReplaySummary:
    asset: MarketAsset
    candles: list[Candle]
    outcomes: list[ReplayOutcome]
    resampled: bool = False


def replay_candles(
    asset: MarketAsset, candles: Iterable[Candle]
) -> ReplaySummary:
    series = list(candles)
    strategy = PriceActionStrategy()
    outcomes: list[ReplayOutcome] = []
    for index, candle in enumerate(series):
        signal = strategy.on_closed_candles(
            asset.symbol,
            series[: index + 1],
            asset_name=asset.display_name,
            payout=asset.payout,
        )
        if signal is None:
            continue
        due_at = int(signal.time.timestamp()) + signal.expiry_seconds
        expiry_index = next(
            (
                future_index
                for future_index in range(index + 1, len(series))
                if series[future_index].time + STRATEGY_SETTINGS.timeframe_seconds
                >= due_at
            ),
            None,
        )
        if expiry_index is None:
            continue
        expiry_price = float(series[expiry_index].close)
        won = (
            expiry_price < signal.price
            if signal.direction == "SELL"
            else expiry_price > signal.price
        )
        outcomes.append(
            ReplayOutcome(
                signal,
                expiry_price,
                "WIN" if won else "LOSS",
                "PROXY",
            )
        )
    return ReplaySummary(asset, series, outcomes)


def wilson_interval(wins: int, count: int) -> tuple[float, float] | None:
    if count <= 0:
        return None
    z = 1.96
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    margin = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count))
    margin /= denominator
    return max(0.0, center - margin) * 100, min(1.0, center + margin) * 100


def _print_summaries(summaries: list[ReplaySummary]) -> None:
    by_setup: dict[str, Counter[str]] = {}
    setup_counts: Counter[str] = Counter()
    observed_hours = 0.0
    assets_with_history = 0
    for summary in summaries:
        if len(summary.candles) > 1:
            span = (
                summary.candles[-1].time
                + STRATEGY_SETTINGS.timeframe_seconds
                - summary.candles[0].time
            )
            observed_hours += max(0.0, span / 3600)
            assets_with_history += 1
        for outcome in summary.outcomes:
            by_setup.setdefault(outcome.signal.setup, Counter())[outcome.result] += 1
            setup_counts[outcome.signal.setup] += 1
    if not assets_with_history:
        print("REPLAY no catalogue assets returned usable candle history.")
        return
    for setup in SETUP_LABELS:
        counts = by_setup.get(setup, Counter())
        total = counts["WIN"] + counts["LOSS"]
        if not total:
            rate_text, interval_text = "n/a", "n/a"
        else:
            rate_text = f"{counts['WIN'] / total * 100:.1f}%"
            interval = wilson_interval(counts["WIN"], total)
            assert interval is not None
            interval_text = f"{interval[0]:.1f}–{interval[1]:.1f}%"
        per_asset_hour = (
            setup_counts[setup] / observed_hours if observed_hours > 0 else 0.0
        )
        print(
            f"REPLAY setup={setup} ({SETUP_LABELS[setup]}) count={total} "
            f"win_rate={rate_text} 95%_Wilson_CI={interval_text} "
            f"signals_per_asset_hour={per_asset_hour:.3f}"
        )
    for summary in summaries:
        status = "resampled_finer_history_to_1m" if summary.resampled else "1m_history"
        print(
            f"REPLAY_ASSET asset={summary.asset.display_name} "
            f"candles={len(summary.candles)} signals={len(summary.outcomes)} "
            f"source={status}"
        )
    print("REPLAY outcomes are approximate candle-close proxies; no parameter tuning was run.")
    print("BREAK-EVEN at 92% payout: 52.1%")


async def run_catalog_replay(
    broker: PocketOptionBroker | None = None,
) -> int:
    market = broker or PocketOptionBroker(config.load_ssid())
    summaries: list[ReplaySummary] = []
    try:
        await market.connect()
        rows = await market.active_assets()
        try:
            payouts = await market.payouts()
        except Exception:
            payouts = {}
        assets = match_runtime_assets(rows, payouts)
        for catalog_asset in CATALOG:
            asset = assets.get(catalog_asset.id)
            if asset is None:
                continue
            builder = CandleBuilder(
                period_seconds=STRATEGY_SETTINGS.timeframe_seconds,
                max_candles=config.REPLAY_MAX_CANDLES_PER_PAIR,
            )
            try:
                history = await market.candle_history(
                    asset.symbol,
                    STRATEGY_SETTINGS.timeframe_seconds,
                    config.REPLAY_MAX_CANDLES_PER_PAIR
                    * STRATEGY_SETTINGS.timeframe_seconds,
                )
                times = sorted(
                    {
                        _normalize_broker_timestamp(
                            row.get("timestamp", row.get("time"))
                        )
                        for row in history
                        if isinstance(row, dict)
                    }
                )
                was_finer = any(
                    0 < right - left < STRATEGY_SETTINGS.timeframe_seconds
                    for left, right in zip(times, times[1:])
                )
                builder.seed(history)
                summary = replay_candles(asset, builder.closed)
                summary.resampled = was_finer
                if summary.candles:
                    summaries.append(summary)
            except (CandleHistoryUnavailable, CandleDataError, TimeoutError) as exc:
                print(
                    f"REPLAY_ASSET asset={asset.display_name} NOT RUN "
                    f"history_unavailable={type(exc).__name__}"
                )
        _print_summaries(summaries)
        return 0
    finally:
        await market.close()
