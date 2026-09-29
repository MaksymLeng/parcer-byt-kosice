from __future__ import annotations

from html import escape
import time

import requests

from .core import Listing, clean_space, utilities_status, is_shared_offer


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, timeout_seconds: int = 20) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds

    def _request(self, method: str, payload: dict, retry: bool = True) -> dict:
        response = requests.post(
            f"{self.base_url}/{method}",
            json=payload,
            timeout=self.timeout_seconds,
        )
        data = response.json()
        if response.status_code == 429 and retry:
            wait_for = int(data.get("parameters", {}).get("retry_after", 2))
            time.sleep(min(wait_for, 30))
            return self._request(method, payload, retry=False)
        if not response.ok or not data.get("ok"):
            description = data.get("description", f"HTTP {response.status_code}")
            raise TelegramError(description)
        return data

    def validate(self) -> dict:
        response = requests.get(f"{self.base_url}/getMe", timeout=self.timeout_seconds)
        data = response.json()
        if not response.ok or not data.get("ok"):
            raise TelegramError(data.get("description", f"HTTP {response.status_code}"))
        return data["result"]

    def updates(self) -> list[dict]:
        response = requests.get(f"{self.base_url}/getUpdates", timeout=self.timeout_seconds)
        data = response.json()
        if not response.ok or not data.get("ok"):
            raise TelegramError(data.get("description", f"HTTP {response.status_code}"))
        return data["result"]

    def send_listing(self, listing: Listing) -> None:
        marker = "⭐ <b>Приоритетный район</b>\n" if listing.preferred_area else ""
        area = f" · {escape(listing.preferred_area)}" if listing.preferred_area else ""
        price = f"{listing.price_eur} €" if listing.price_eur is not None else "цена не указана"
        status = utilities_status(listing.text)
        utilities = {
            "included": "✅ коммунальные включены",
            "extra": "⚠️ коммунальные оплачиваются отдельно",
            "unknown": "❔ коммунальные: уточнить",
        }[status]
        summary = clean_space(listing.description)
        price_note = (listing.metadata or {}).get("price_note", "")
        if price_note:
            summary = price_note + " " + summary
        if len(summary) > 520:
            summary = summary[:517].rstrip() + "…"
        caption = (
            f"{marker}{'🏠 <b>Подселение / комната</b>' + chr(10) if is_shared_offer(listing.text) or (listing.metadata or {}).get('property_type', '').lower() in {'izba', 'izby', 'spolubývanie'} else ''}<b>{escape(listing.title)}</b>\n\n"
            f"💶 <b>{escape(price)}</b>\n"
            f"{utilities}\n"
            f"📍 {escape(listing.location)}{area}\n"
            f"🔎 {escape(listing.source.upper())}\n\n"
            f"📅 {escape(listing.published_at or 'дата не указана')}\n\n"
            f"{escape(summary)}"
        )
        keyboard = {"inline_keyboard": [[{"text": "Открыть объявление", "url": listing.url}]]}
        common = {
            "chat_id": self.chat_id,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }
        if listing.image_url:
            try:
                self._request("sendPhoto", {**common, "photo": listing.image_url, "caption": caption})
                return
            except TelegramError:
                pass
        self._request(
            "sendMessage",
            {**common, "text": caption, "link_preview_options": {"is_disabled": True}},
        )

    def send_status(self, text: str) -> None:
        self._request('sendMessage', {'chat_id': self.chat_id, 'text': text,
                                     'link_preview_options': {'is_disabled': True}})
