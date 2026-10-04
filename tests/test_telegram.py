import os

import httpx
import pytest
import respx
from pydantic import SecretStr

from stockapp.alerts.telegram import (
    API_BASE,
    AlertError,
    PrivacyViolation,
    check_summary_only,
    find_chat_ids,
    send_message,
)
from stockapp.config import Settings

TOKEN = "123:secret-token"
URL = f"{API_BASE}/bot{TOKEN}/sendMessage"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, telegram_token=SecretStr(TOKEN), telegram_chat_id="42")


@respx.mock
def test_sends_plain_text(settings: Settings):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    send_message("Weekly plan ready: 2 actions", settings=settings)
    body = route.calls.last.request.read().decode()
    assert '"chat_id":"42"' in body
    assert "Weekly plan ready" in body


@pytest.mark.parametrize(
    "text",
    ["Sold for ₹1,265", "value Rs. 30000", "value Rs 300", "INR 4000 invested", "₹ 15"],
)
def test_rupee_amounts_are_refused(text: str):
    with pytest.raises(PrivacyViolation):
        check_summary_only(text)


@pytest.mark.parametrize("text", ["2 actions this week: SELL XYZ, BUY ABC", "Data OK, score 97"])
def test_summaries_pass(text: str):
    check_summary_only(text)


@respx.mock
def test_retries_on_rate_limit_then_succeeds(settings: Settings):
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    send_message("hi", settings=settings, backoff_s=0)
    assert route.call_count == 2


@respx.mock
def test_client_error_raises_without_leaking_token(settings: Settings):
    respx.post(URL).mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    with pytest.raises(AlertError) as exc:
        send_message("hi", settings=settings, backoff_s=0)
    assert "chat not found" in str(exc.value)
    assert TOKEN not in str(exc.value)


@respx.mock
def test_network_failure_raises_after_retries(settings: Settings):
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AlertError):
        send_message("hi", settings=settings, retries=2, backoff_s=0)
    assert route.call_count == 3


def test_unconfigured_raises():
    with pytest.raises(AlertError):
        send_message("hi", settings=Settings(_env_file=None, telegram_token=None))


@pytest.mark.live
@pytest.mark.skipif(
    not (os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
    reason="TELEGRAM_TOKEN and TELEGRAM_CHAT_ID not set",
)
def test_live_send():
    send_message("stockapp live test from pytest")


@respx.mock
def test_find_chat_ids_returns_private_chats_only(settings: Settings):
    respx.get(f"{API_BASE}/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {"message": {"chat": {"id": 512, "type": "private", "first_name": "Ravi"}}},
                    {"message": {"chat": {"id": 512, "type": "private", "first_name": "Ravi"}}},
                    {"message": {"chat": {"id": -100, "type": "group", "title": "x"}}},
                    {"edited_message": {"chat": {"id": 7, "type": "private"}}},
                ],
            },
        )
    )
    assert find_chat_ids(settings=settings) == [("512", "Ravi")]


@respx.mock
def test_find_chat_ids_bad_token(settings: Settings):
    respx.get(f"{API_BASE}/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with pytest.raises(AlertError, match="Unauthorized"):
        find_chat_ids(settings=settings)
