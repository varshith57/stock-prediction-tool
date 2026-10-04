"""Command-line entry point: ``uv run stockapp <command>``."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from stockapp.alerts.telegram import AlertError, find_chat_ids, send_message
from stockapp.config import get_app_config, get_settings

IST = ZoneInfo("Asia/Kolkata")


def _telegram_chat_id(_: argparse.Namespace) -> int:
    try:
        chats = find_chat_ids()
    except AlertError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if not chats:
        print("No chats found. Open your bot in Telegram, send it a message, then run this again.")
        return 1
    for chat_id, name in chats:
        print(f"TELEGRAM_CHAT_ID={chat_id}    ({name})")
    print("\nCopy that line into your .env file.")
    return 0


def _telegram_test(_: argparse.Namespace) -> int:
    now = datetime.now(IST).strftime("%a %d %b %Y, %H:%M IST")
    try:
        send_message(
            f"stockapp test alert ({get_settings().app_env}) at {now}. Alerts are working."
        )
    except AlertError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print("Sent. Check Telegram.")
    return 0


def _config_show(_: argparse.Namespace) -> int:
    print(get_app_config().model_dump_json(indent=2))
    s = get_settings()
    print(f"\nenv={s.app_env} lake={s.lake_uri} telegram_configured={s.telegram_configured}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockapp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("telegram-test", help="send a test Telegram message").set_defaults(
        func=_telegram_test
    )
    sub.add_parser(
        "telegram-chat-id", help="print your chat ID (after you message the bot once)"
    ).set_defaults(func=_telegram_chat_id)
    sub.add_parser("config", help="print product config and environment wiring").set_defaults(
        func=_config_show
    )
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
