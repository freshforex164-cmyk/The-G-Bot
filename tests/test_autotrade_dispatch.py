"""Auto-Trade vs Signal Mode: confirmed signals go to auto-traders' bots at once, never as text."""
import asyncio
import unittest
from types import SimpleNamespace

from autotrade_client import AutotradeRoster
from telegram_bot import SubscriberFanoutNotifier
from tests.test_signal_card import make_signal


class Recorder:
    def __init__(self, reason="queued"):
        self.calls, self.reason = [], reason

    async def submit_signal(self, uid, payload):
        self.calls.append((uid, payload))
        return {"accepted": self.reason == "queued", "reason": self.reason}


def build(subscribed, auto_ids, reason="queued"):
    sent = []

    async def send_message(chat_id, text):
        sent.append(chat_id)
    autotrade = Recorder(reason)
    roster = AutotradeRoster(autotrade)
    roster._ids, roster._fetched_at = set(auto_ids), roster._clock()
    fanout = SubscriberFanoutNotifier(SimpleNamespace(send_message=send_message),
                                      SimpleNamespace(subscribed_chat_ids=lambda pair: list(subscribed)),
                                      max_messages_per_second=1000, autotrade=autotrade, roster=roster)
    return fanout, autotrade, roster, sent


async def settle(fanout):
    for _ in range(50):
        if not fanout._autotrade_tasks:
            return
        await asyncio.gather(*list(fanout._autotrade_tasks))
        await asyncio.sleep(0)            # let done-callbacks remove finished tasks


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_hand_off_starts_before_any_card_is_sent(self):
        fanout, autotrade, _, sent = build([111], [222])
        await fanout.dispatch_autotrade(make_signal())       # the scanner callback, card queue not run yet
        await settle(fanout)
        self.assertEqual([u for u, _ in autotrade.calls], [222])
        self.assertEqual(sent, [])

    async def test_every_auto_trader_gets_every_signal_even_for_another_chosen_asset(self):
        fanout, autotrade, _, sent = build([111], [222, 333, -100500])   # 222/333 chose other charts
        await fanout.send(make_signal())
        await settle(fanout)
        self.assertEqual(sorted(u for u, _ in autotrade.calls), [222, 333])   # never groups
        self.assertEqual(sent, [111])                                          # Signal Mode card only

    async def test_auto_trader_subscribed_to_this_asset_gets_no_text_signal(self):
        fanout, autotrade, _, sent = build([111, 222], [222])
        signal = make_signal()
        fanout.dispatch_autotrade(signal)
        await fanout.send(signal)
        await settle(fanout)
        self.assertEqual(sent, [111])
        self.assertEqual(len(autotrade.calls), 1)               # dispatched once per signal id
        payload = autotrade.calls[0][1]
        self.assertEqual((payload["asset"], payload["expiry_seconds"]), (signal.pair, 60))

    async def test_without_auto_trade_everyone_gets_the_signal_card(self):
        fanout, autotrade, _, sent = build([111, 222], [])
        await fanout.send(make_signal())
        await settle(fanout)
        self.assertEqual((sent, autotrade.calls), ([111, 222], []))

    async def test_stopped_bot_falls_back_to_card_only_for_its_chosen_asset(self):
        fanout, _, roster, sent = build([111], [222], reason="not_running")   # 222 chose another asset
        await fanout.send(make_signal())
        await settle(fanout)
        self.assertEqual(sent, [111])
        self.assertFalse(roster.is_active(222))               # back to Signal Mode for the next signal


class StatusTests(unittest.TestCase):
    def test_status_explains_signals_that_were_not_placed(self):
        from autotrade_commands import stats_text
        text = stats_text({"mode": "demo", "bot": {"running": True, "signals_received": 9,
                                                   "skipped": {"payout_below_minimum": 3, "max_concurrent": 1}},
                           "sessions": {}, "today": {}, "all_time": {}, "settings": {}})
        self.assertIn("Signals received: 9", text)
        self.assertIn("Not placed: payout under minimum 3 · max open trades 1", text)


if __name__ == "__main__":
    unittest.main()
