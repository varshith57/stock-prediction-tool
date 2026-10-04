"""Telegram alerts: summaries only.

Product rule: alerts never carry holdings values or quantities. ``send_message`` enforces the
money part in code by refusing any text with a rupee amount, so a caller bug can't leak
portfolio values into a chat.
"""

from __future__ import annotations

import re
import time

import httpx

from stockapp.config import Settings, get_settings

API_BASE = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram's limit for a single message
_MONEY = re.compile(r"(₹|\bRs\.?|\bINR)\s*\d", re.IGNORECASE)


class AlertError(RuntimeError):
    pass


class PrivacyViolation(AlertError):
    """The message looks like it contains a rupee amount."""


def check_summary_only(text: str) -> None:
    if _MONEY.search(text):
        raise PrivacyViolation("alert text contains a rupee amount; alerts are summaries only")


def send_message(
    text: str,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
    retries: int = 3,
    backoff_s: float = 1.0,
) -> None:
    """Send ``text`` to the configured chat. Raises ``AlertError`` if it can't be delivered."""
    settings = settings or get_settings()
    if not settings.telegram_configured:
        raise AlertError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set")
    check_summary_only(text)
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1] + "…"

    assert settings.telegram_token is not None
    url = f"{API_BASE}/bot{settings.telegram_token.get_secret_value()}/sendMessage"
    payload = {"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True}

    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        for attempt in range(retries + 1):
            try:
                resp = client.post(url, json=payload)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise AlertError(f"Telegram unreachable: {type(exc).__name__}") from None
                time.sleep(backoff_s * 2**attempt)
                continue
            if resp.status_code == 200:
                return
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == retries:
                    break
                retry_after = _retry_after(resp)
                time.sleep(retry_after if retry_after is not None else backoff_s * 2**attempt)
                continue
            break
        # Never include the URL: it contains the bot token.
        raise AlertError(
            f"Telegram rejected the message: HTTP {resp.status_code} {_description(resp)}"
        )
    finally:
        if own_client:
            client.close()


def find_chat_ids(
    *, settings: Settings | None = None, client: httpx.Client | None = None
) -> list[tuple[str, str]]:
    """Return ``(chat_id, display name)`` for private chats that have messaged the bot.

    Setup helper only: the app never reads incoming messages in normal operation.
    """
    settings = settings or get_settings()
    if settings.telegram_token is None:
        raise AlertError("TELEGRAM_TOKEN must be set in .env")
    url = f"{API_BASE}/bot{settings.telegram_token.get_secret_value()}/getUpdates"
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        try:
            resp = client.get(url)
        except httpx.TransportError as exc:
            raise AlertError(f"Telegram unreachable: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise AlertError(f"Telegram refused: HTTP {resp.status_code} {_description(resp)}")
        found: dict[str, str] = {}
        for update in resp.json().get("result", []):
            chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat", {})
            if chat.get("type") == "private" and "id" in chat:
                name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                found[str(chat["id"])] = name or chat.get("username", "")
        return list(found.items())
    finally:
        if own_client:
            client.close()


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.json()["parameters"]["retry_after"])
    except (ValueError, KeyError, TypeError):
        return None


def _description(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("description", ""))
    except ValueError:
        return ""
