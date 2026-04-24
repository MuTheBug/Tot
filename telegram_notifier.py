"""Minimal Telegram notifier. Never raises into the trading loop."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    def send(self, text: str, silent: bool = False) -> None:
        if not self.token or not self.chat_id:
            log.info("[TG disabled] %s", text)
            return
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        for attempt in range(3):
            try:
                r = requests.post(f"{self.base}/sendMessage", json=payload, timeout=10)
                if r.status_code == 200:
                    return
                log.warning("Telegram send failed (%s): %s", r.status_code, r.text)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram network error: %s", e)
            time.sleep(1.5 * (attempt + 1))

    def event(self, title: str, body: Optional[str] = None, emoji: str = "ℹ️") -> None:
        msg = f"{emoji} <b>{title}</b>"
        if body:
            msg += f"\n{body}"
        self.send(msg)

    def send_photo(self, image_bytes: bytes, caption: str = "", silent: bool = False) -> None:
        """Send a PNG image via sendPhoto. Falls back to text on failure."""
        if not self.token or not self.chat_id:
            log.info("[TG disabled] photo (%d bytes) caption=%s", len(image_bytes), caption)
            return
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {
            "chat_id": self.chat_id,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        for attempt in range(3):
            try:
                r = requests.post(f"{self.base}/sendPhoto", data=data, files=files, timeout=20)
                if r.status_code == 200:
                    return
                log.warning("Telegram sendPhoto failed (%s): %s", r.status_code, r.text)
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram photo network error: %s", e)
            time.sleep(1.5 * (attempt + 1))
        # Final fallback: send the caption as a regular message so we don't
        # silently lose the alert.
        if caption:
            self.send(caption)
