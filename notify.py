"""Telegram notifier. Enabled when TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
import os
from datetime import datetime, timezone

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATUS = {"enabled": bool(TOKEN and CHAT_ID), "sent": 0,
          "last_sent": None, "last_error": None}


def enabled():
    return bool(TOKEN and CHAT_ID)


def send(text):
    """Send a Telegram message. Best-effort; errors recorded, never raised."""
    if not enabled():
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=(5, 10),
        )
        r.raise_for_status()
        STATUS["sent"] += 1
        STATUS["last_sent"] = datetime.now(timezone.utc).isoformat()
        STATUS["last_error"] = None
        return True
    except Exception as e:  # noqa: BLE001
        STATUS["last_error"] = str(e)[:200]
        return False
