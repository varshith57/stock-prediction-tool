"""Command-line entry point: ``uv run stockapp <command>``."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
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


def _db_migrate(_: argparse.Namespace) -> int:
    from stockapp.db import connect, migrate
    from stockapp.ingest.registry import sync_registry

    with connect() as conn:
        applied = migrate(conn)
        n = sync_registry(conn)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    print(f"Source registry synced: {n} source(s)")
    return 0


def _parse_day(s: str) -> date:
    return date.fromisoformat(s)


def _ingest(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.http import PoliteClient
    from stockapp.ingest.nse_udiff import NseUdiffBhavcopy
    from stockapp.lake import Lake

    connectors = {"nse-udiff": NseUdiffBhavcopy}
    start, end = args.start, args.end or args.start
    if end < start:
        print("--end is before --start", file=sys.stderr)
        return 2
    failed = 0
    with connect() as conn, PoliteClient(min_interval_s=args.interval) as http:
        connector = connectors[args.source](conn, Lake.from_settings(), http)
        day = start
        while day <= end:
            if day.weekday() < 5 or args.include_weekends:
                r = connector.run(day, force=args.force)
                extra = f" rows={r.rows}" if r.rows else ""
                print(f"{r.partition_key} {r.status}{extra} {r.message}".rstrip())
                failed += r.status == "failed"
                if http.consecutive_failures >= http.breaker_threshold:
                    print("Stopping: source keeps failing (circuit open).", file=sys.stderr)
                    return 1
            day += timedelta(days=1)
    return 1 if failed else 0


def _calendar_refresh(_: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.calendar import refresh_holidays
    from stockapp.ingest.http import PoliteClient
    from stockapp.lake import Lake

    with connect() as conn, PoliteClient() as http:
        n = refresh_holidays(conn, Lake.from_settings(), http)
    print(f"Loaded {n} NSE capital-market holidays.")
    return 0


def _calendar_show(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.calendar import TradingCalendar

    end = args.end or args.start
    with connect() as conn:
        cal = TradingCalendar(conn, args.start, end)
        day = args.start
        while day <= end:
            i = cal.info(day)
            print(
                f"{day} {day:%a} {i.status:<9} {'trading' if i.is_trading_day else '-':<8} {i.note}"
            )
            day += timedelta(days=1)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockapp", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "migrate", help="apply database migrations and sync the source registry"
    ).set_defaults(func=_db_migrate)

    ing = sub.add_parser("ingest", help="fetch, archive, validate and load daily files")
    ing.add_argument("source", choices=["nse-udiff"])
    ing.add_argument("--start", type=_parse_day, required=True, help="YYYY-MM-DD")
    ing.add_argument("--end", type=_parse_day, help="YYYY-MM-DD (default: same as --start)")
    ing.add_argument("--force", action="store_true", help="reload even if already loaded")
    ing.add_argument("--include-weekends", action="store_true", help="also try Sat/Sun")
    ing.add_argument("--interval", type=float, default=1.5, help="seconds between requests")
    ing.set_defaults(func=_ingest)

    cal = sub.add_parser("calendar", help="NSE trading calendar")
    cal_sub = cal.add_subparsers(dest="calendar_command", required=True)
    cal_sub.add_parser("refresh", help="load NSE's current holiday list").set_defaults(
        func=_calendar_refresh
    )
    show = cal_sub.add_parser("show", help="show why each date is or isn't a trading day")
    show.add_argument("--start", type=_parse_day, required=True)
    show.add_argument("--end", type=_parse_day)
    show.set_defaults(func=_calendar_show)

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
