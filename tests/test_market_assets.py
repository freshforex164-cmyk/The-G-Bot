from __future__ import annotations

import unittest

from market_assets import CATALOG, CATEGORIES, match_runtime_assets, normalize_asset_name


class MarketAssetTests(unittest.TestCase):
    def test_catalogue_has_five_categories_and_all_requested_assets(self) -> None:
        self.assertEqual(CATEGORIES, ("Currencies", "Crypto", "Commodities", "Stocks", "Indices"))
        self.assertEqual(len(CATALOG), 35)
        self.assertEqual(CATALOG[0].name, "AED/CNY")
        self.assertEqual(CATALOG[-1].name, "100GBP OTC")

    def test_matching_ignores_case_slashes_spaces_and_otc_suffix(self) -> None:
        matches = match_runtime_assets(
            [
                {"symbol": "aEd_cNy_OTC", "name": "AED/CNY OTC", "is_active": True},
                {"symbol": "GOLD_otc", "name": "Gold OTC", "is_active": False},
            ],
            {"aEd_cNy_OTC": 0, "GOLD_otc": 93},
        )
        self.assertEqual(matches["aed-cny"].symbol, "aEd_cNy_OTC")
        self.assertEqual(matches["aed-cny"].payout, 0)
        self.assertFalse(matches["gold"].is_open)
        self.assertEqual(normalize_asset_name("Johnson & Johnson OTC"), "johnsonjohnson")

    def test_aliases_match_variations_without_payout_or_otc_filtering(self) -> None:
        matches = match_runtime_assets(
            [
                {"symbol": "Facebook_otc", "name": "Facebook OTC", "is_active": True},
                {"symbol": "XAUUSD", "name": "Gold", "is_active": True},
            ]
        )
        self.assertEqual(matches["facebook-inc"].symbol, "Facebook_otc")
        self.assertIsNone(matches["facebook-inc"].payout)
        self.assertEqual(matches["gold"].symbol, "XAUUSD")

    def test_unmatched_names_are_omitted_for_audit_reporting(self) -> None:
        matches = match_runtime_assets(
            [{"symbol": "UNKNOWN_otc", "name": "Unknown OTC", "is_active": True}]
        )
        self.assertEqual(matches, {})


if __name__ == "__main__":
    unittest.main()
