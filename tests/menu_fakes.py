"""Shared fakes for Telegram menu tests (no network)."""

from __future__ import annotations

from market_assets import MarketAsset
from notifications import TelegramSendError

FAKE_DEMO_SSID = '42["auth",{"session":"x","uid":1,"isDemo":1}]'
FAKE_REAL_SSID = '42["auth",{"session":"x","uid":1,"isDemo":0}]'


class MenuClient:
    """Tracks which bot messages are visible in each chat."""

    def __init__(self) -> None:
        self.next_id = 1
        self.visible: dict[int, dict[int, dict]] = {}
        self.edits: list[int] = []
        self.deletes: list[int] = []
        self.callbacks: list[str] = []
        self.fail_next_edit = False

    def menus(self, chat_id: int) -> list[dict]:
        return [m for m in self.visible.get(chat_id, {}).values() if m["markup"]]

    async def send_message(self, chat_id, text, *, reply_markup=None, parse_mode="HTML"):
        message_id = self.next_id
        self.next_id += 1
        self.visible.setdefault(chat_id, {})[message_id] = {
            "id": message_id,
            "text": text,
            "markup": reply_markup,
        }
        return {"message_id": message_id}

    async def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None, parse_mode="HTML"):
        if self.fail_next_edit:
            self.fail_next_edit = False
            raise TelegramSendError("Telegram rejected the message (400).", status_code=400)
        message = self.visible.get(chat_id, {}).get(message_id)
        if message is None:
            raise TelegramSendError("Telegram rejected the message (400).", status_code=400)
        self.edits.append(message_id)
        message["text"] = text
        message["markup"] = reply_markup

    async def delete_message(self, chat_id, message_id):
        self.deletes.append(message_id)
        self.visible.get(chat_id, {}).pop(message_id, None)

    async def answer_callback(self, callback_id):
        self.callbacks.append(callback_id)


class FakeScanner:
    def __init__(self, available=("aud-cad-otc",), payouts=None) -> None:
        self.available = set(available)
        self.payouts = dict(payouts or {"AUDCAD_otc": 92.0})
        self.asset_matches = {
            "aud-cad-otc": MarketAsset("aud-cad-otc", "Currencies", "AUD/CAD OTC", "AUDCAD_otc", None, True),
            "aud-cad": MarketAsset("aud-cad", "Currencies", "AUD/CAD", "AUDCAD", None, True),
            "gbp-usd-otc": MarketAsset("gbp-usd-otc", "Currencies", "GBP/USD OTC", "GBPUSD_otc", None, True),
        }
        self.pairs: dict = {}
        self.signals: dict = {}
        self.selections: list[tuple[int, str]] = []

    async def live_payouts(self):
        return dict(self.payouts)

    async def live_payout(self, symbol):
        return self.payouts.get(symbol)

    async def select_asset(self, chat_id, asset_id):
        self.selections.append((chat_id, asset_id))
        return self.asset_matches[asset_id] if asset_id in self.available else None


def start(chat_id: int = 5) -> dict:
    return {"message": {"chat": {"id": chat_id}, "from": {"id": chat_id}, "text": "/start"}}


def tap(data: str, menu_id: int, chat_id: int = 5, callback_id: str = "cb") -> dict:
    return {
        "callback_query": {
            "id": callback_id,
            "data": data,
            "message": {"message_id": menu_id, "chat": {"id": chat_id}},
        }
    }
