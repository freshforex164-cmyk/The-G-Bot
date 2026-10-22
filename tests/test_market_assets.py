from __future__ import annotations

import unittest

from market_assets import eligible_assets, normalize_assets


class MarketAssetTests(unittest.TestCase):
    def test_only_named_otc_rows_with_provider_names_are_normalized(self) -> None:
        assets = normalize_assets(
            [
                {"symbol": "EURGBP_otc", "name": "EUR/GBP OTC", "is_active": True, "is_otc": True},
                {"symbol": "EURGBP", "name": "EUR/GBP", "is_active": True},
                {"symbol": "NO_NAME_otc", "is_active": True, "is_otc": True},
                {"symbol": "CLOSED_otc", "name": "Closed OTC", "is_active": False, "is_otc": True},
            ],
            {"EURGBP_otc": 92, "CLOSED_otc": 95},
        )
        self.assertEqual(assets["EURGBP_otc"].display_name, "EUR/GBP OTC")
        self.assertEqual(set(assets), {"EURGBP_otc", "CLOSED_otc"})
        self.assertFalse(assets["CLOSED_otc"].is_open)

    def test_eligibility_requires_open_and_minimum_payout(self) -> None:
        assets = normalize_assets(
            [
                {"symbol": "OPEN_otc", "name": "Open OTC", "is_active": True, "is_otc": True},
                {"symbol": "LOW_otc", "name": "Low OTC", "is_active": True, "is_otc": True},
                {"symbol": "CLOSED_otc", "name": "Closed OTC", "is_active": False, "is_otc": True},
            ],
            {"OPEN_otc": 80, "LOW_otc": 79, "CLOSED_otc": 95},
        )
        selected = eligible_assets(assets, 80, 20)
        self.assertEqual([item.symbol for item in selected], ["OPEN_otc"])


if __name__ == "__main__":
    unittest.main()
