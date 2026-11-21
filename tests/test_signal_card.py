"""Signal card layout, the six setup names, and hand-off of delivered signals to auto-trading."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from autotrade_commands import stats_text
from notifications import format_signal_message
from strategy.model import Signal
from strategy.price_action import SETUP_LABELS, setup_label
from telegram_core import SubscriberFanoutNotifier


def make_signal(direction="BUY", setup="UP_CONTINUATION_BUY", payout=92.0):
    now = datetime.now(timezone.utc)
    return Signal(pair="DOGE_otc", direction=direction, setup=setup, price=0.1, time=now,
                  asset_name="Dogecoin OTC", payout=payout, valid_until=now + timedelta(seconds=10))


class SignalCardTests(unittest.TestCase):
    def test_buy_card_has_exact_layout_with_setup(self):
        self.assertEqual(
            format_signal_message(make_signal()),
            "<b>🟢 BUY / CALL</b>\n<b>Setup:</b> Uptrend Continuation\n<b>Asset:</b> Dogecoin OTC\n"
            "<b>Expiration:</b> 1 minute\n<b>Payout:</b> 92%\n<i>Payouts may vary.</i>",
        )

    def test_sell_card_is_red_and_shows_its_setup(self):
        card = format_signal_message(make_signal("SELL", "RANGE_HIGH_SELL"))
        self.assertTrue(card.startswith("<b>🔴 SELL / PUT</b>\n<b>Setup:</b> Consolidation High Rejection\n"))

    def test_the_six_market_setups_have_their_exact_names(self):
        self.assertEqual(sorted(SETUP_LABELS.values()), sorted([
            "Uptrend Continuation", "Uptrend Pullback", "Consolidation High Rejection",
            "Consolidation Low Rejection", "Downtrend Continuation", "Downtrend Pullback"]))
        self.assertEqual(setup_label("DOWN_PULLBACK_BUY"), "Downtrend Pullback")
        self.assertEqual(setup_label("UP_PULLBACK_SELL"), "Uptrend Pullback")


class FakeAutotrade:
    def __init__(self):
        self.calls = []

    async def submit_signal(self, uid, payload):
        self.calls.append((uid, payload))
        return {"accepted": True, "reason": "queued"}


class AutotradeHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivered_signal_goes_to_each_private_subscriber_bot(self):
        sent = []

        async def send_message(chat_id, text):
            sent.append(chat_id)
        client = SimpleNamespace(send_message=send_message)
        store = SimpleNamespace(subscribed_chat_ids=lambda pair: [111, -100200, 222])
        autotrade = FakeAutotrade()
        fanout = SubscriberFanoutNotifier(client, store, max_messages_per_second=1000, autotrade=autotrade)
        signal = make_signal("SELL", "DOWN_CONTINUATION_SELL")
        await fanout.send(signal)
        await asyncio.gather(*list(fanout._autotrade_tasks))
        self.assertEqual(sent, [111, -100200, 222])
        self.assertEqual(sorted(uid for uid, _ in autotrade.calls), [111, 222])   # never group chats
        payload = autotrade.calls[0][1]
        self.assertEqual((payload["signal_id"], payload["asset"], payload["direction"], payload["expiry_seconds"]),
                         (signal.id, "DOGE_otc", "SELL", 60))
        self.assertEqual(fanout.autotrade_accepted, 2)


class StatsTextTests(unittest.TestCase):
    def test_stats_show_balance_wins_losses_and_break_even(self):
        text = stats_text({
            "mode": "demo", "bot": {"running": True, "open_trades": 0, "balance": 10050.5, "balance_account": "demo"},
            "sessions": {"demo": {"status": "active"}, "real": {"status": "missing"}},
            "today": {"trades": 4, "wins": 2, "losses": 1, "draws": 1, "realized_pnl": 0.7},
            "all_time": {"trades": 4, "wins": 2, "losses": 1, "draws": 1, "net_profit": 0.7, "win_rate": 66.7},
            "settings": {"follow_signals": True},
        })
        self.assertIn("Balance (Demo): 10,050.50", text)
        self.assertIn("Today: 4 trades · 2 wins · 1 losses · 1 break-even · P/L +0.70", text)
        self.assertIn("Win rate: 66.7%", text)
