"""Catalogue loading and runtime Pocket Option symbol matching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CatalogAsset:
    id: str
    category: str
    name: str
    aliases: tuple[str, ...] = ()
    # Exact Pocket Option symbol (e.g. AUDCAD, AUDCAD_otc, SOL-USD_otc).
    symbol: str | None = None


@dataclass(frozen=True)
class MarketAsset:
    catalog_id: str
    category: str
    display_name: str
    symbol: str
    payout: float | None
    is_open: bool


def normalize_asset_name(value: str) -> str:
    """Trim surrounding whitespace without changing spelling or OTC identity."""
    return value.strip()


def load_catalog(path: str | Path = "assets.json") -> tuple[CatalogAsset, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    result: list[CatalogAsset] = []
    for category in payload["categories"]:
        for item in category["assets"]:
            result.append(
                CatalogAsset(
                    id=str(item["id"]),
                    category=str(category["name"]),
                    name=str(item["name"]),
                    aliases=tuple(map(str, item.get("aliases", ()))),
                    symbol=(str(item["symbol"]) if item.get("symbol") else None),
                )
            )
    return tuple(result)


CATALOG = load_catalog()
CATALOG_BY_ID = {asset.id: asset for asset in CATALOG}
CATEGORIES = tuple(dict.fromkeys(asset.category for asset in CATALOG))


def match_runtime_assets(
    rows: list[dict[str, Any]], payouts: dict[str, float] | None = None
) -> dict[str, MarketAsset]:
    """Match exact display names/aliases to one live broker symbol each."""
    payouts = payouts or {}
    result: dict[str, MarketAsset] = {}
    row_symbols = {row.get("symbol") for row in rows if isinstance(row, dict)}
    for row in rows:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        provider_name = row.get("name")
        provider_names = (
            [provider_name]
            if isinstance(provider_name, str) and provider_name.strip()
            else []
        )
        if not provider_names:
            provider_names = [symbol]
        live_symbols = row_symbols
        for asset in CATALOG:
            names = (asset.name, *asset.aliases)
            # The exact catalogue symbol wins; display names are only a
            # fallback when the broker does not list that exact symbol.
            if asset.symbol is not None and asset.symbol in live_symbols:
                if symbol != asset.symbol:
                    continue
            elif not any(
                normalize_asset_name(provider) == name
                for provider in provider_names
                for name in names
            ):
                continue
            payout_value = row.get("payout", payouts.get(symbol))
            try:
                payout = float(payout_value) if payout_value is not None else None
            except (TypeError, ValueError, OverflowError):
                payout = None
            if payout is not None and not 0 <= payout <= 100:
                payout = None
            candidate = MarketAsset(
                catalog_id=asset.id,
                category=asset.category,
                display_name=asset.name,
                symbol=symbol,
                payout=payout,
                is_open=bool(row.get("is_active", row.get("is_open", False))),
            )
            current = result.get(asset.id)
            if current is None or (candidate.is_open and not current.is_open):
                result[asset.id] = candidate
            break
    return result


def asset_table(
    matches: dict[str, MarketAsset], *, include_closed: bool = True
) -> list[tuple[str, str | None]]:
    return [
        (
            asset.name,
            matches[asset.id].symbol if asset.id in matches else None,
        )
        for asset in CATALOG
        if include_closed or (asset.id in matches and matches[asset.id].is_open)
    ]
