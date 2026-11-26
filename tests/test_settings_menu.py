import unittest

from autotrade_commands import settings_menu_markup


class SettingsMenuTests(unittest.TestCase):
    def test_menu_offers_stake_and_stop_balance_buttons(self):
        data = {b["callback_data"] for r in settings_menu_markup()["inline_keyboard"] for b in r}
        self.assertIn("at:set:stake:5", data)
        self.assertIn("at:set:stop_balance:0", data)
        self.assertIn("at:set:stake_mode:martingale", data)

    def test_every_button_fits_telegram_limit(self):
        for r in settings_menu_markup()["inline_keyboard"]:
            for b in r:
                self.assertLessEqual(len(b["callback_data"].encode()), 64)


if __name__ == "__main__":
    unittest.main()
