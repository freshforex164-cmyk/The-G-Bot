from __future__ import annotations

import unittest

from market_assets import CATALOG, CATEGORIES, match_runtime_assets, normalize_asset_name


class MarketAssetTests(unittest.TestCase):
    def test_catalogue_has_five_categories_and_separate_regular_and_otc_assets(self) -> None:
        self.assertEqual(CATEGORIES, ("Currencies", "Crypto", "Commodities", "Stocks", "Indices"))
        self.assertEqual(len(CATALOG), 56)
        self.assertEqual(CATALOG[0].name, "AED/CNY")
        self.assertEqual(CATALOG[-1].name, "100GBP OTC")
        self.assertEqual(
            {asset.name for asset in CATALOG if asset.name.startswith("GBP/USD")},
            {"GBP/USD", "GBP/USD OTC"},
        )

    def test_matching_preserves_exact_case_and_otc_suffix(self) -> None:
        matches = match_runtime_assets(
            [
                {"symbol": "AEDCNY_otc", "name": "AED/CNY OTC", "is_active": True},
                {"symbol": "AEDCNY", "name": "AED/CNY", "is_active": True},
                {"symbol": "GOLD_otc", "name": "Gold OTC", "is_active": False},
            ],
            {"AEDCNY_otc": 0, "AEDCNY": 87, "GOLD_otc": 93},
        )
        self.assertEqual(matches["aed-cny-otc"].symbol, "AEDCNY_otc")
        self.assertEqual(matches["aed-cny-otc"].payout, 0)
        self.assertEqual(matches["aed-cny"].symbol, "AEDCNY")
        self.assertEqual(matches["aed-cny"].payout, 87)
        self.assertFalse(matches["gold"].is_open)
        self.assertEqual(normalize_asset_name("Johnson & Johnson OTC"), "Johnson & Johnson OTC")
        self.assertNotEqual(
            normalize_asset_name("GBP/USD"),
            normalize_asset_name("GBP/USD OTC"),
        )

    def test_exact_display_names_match_and_case_mismatches_do_not(self) -> None:
        matches = match_runtime_assets(
            [
                {"symbol": "Facebook_otc", "name": "FACEBOOK INC OTC", "is_active": True},
                {"symbol": "XAUUSD", "name": "Gold OTC", "is_active": True},
                {"symbol": "AUS200", "name": "AUS 200", "is_active": True},
            ]
        )
        self.assertEqual(matches["facebook-inc"].symbol, "Facebook_otc")
        self.assertIsNone(matches["facebook-inc"].payout)
        self.assertEqual(matches["gold"].symbol, "XAUUSD")
        self.assertNotIn("aus-200-otc", matches)

    def test_unmatched_names_are_omitted_for_audit_reporting(self) -> None:
        matches = match_runtime_assets(
            [{"symbol": "UNKNOWN_otc", "name": "Unknown OTC", "is_active": True}]
        )
        self.assertEqual(matches, {})

    def test_duplicate_live_names_resolve_to_one_open_symbol(self) -> None:
        matches = match_runtime_assets(
            [
                {"symbol": "GOLD_CLOSED", "name": "Gold OTC", "is_active": False},
                {"symbol": "GOLD_OPEN", "name": "Gold OTC", "is_active": True},
            ]
        )
        self.assertEqual(len([item for item in matches if item == "gold"]), 1)
        self.assertEqual(matches["gold"].symbol, "GOLD_OPEN")


if __name__ == "__main__":
    unittest.main()
