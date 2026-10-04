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


def _set_password(_: argparse.Namespace) -> int:
    import getpass

    from stockapp.auth import hash_password

    first = getpass.getpass("New app password (10+ characters): ")
    if first != getpass.getpass("Repeat: "):
        print("Passwords don't match.", file=sys.stderr)
        return 1
    try:
        hashed = hash_password(first)
    except ValueError as exc:
        print(f"Not set: {exc}", file=sys.stderr)
        return 1
    print("\nAdd this line to .env (and to Streamlit secrets when deploying):\n")
    print(f"APP_PASSWORD_HASH={hashed}")
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


def _connectors() -> dict:
    from stockapp.ingest.nse_corp_actions import NseCorporateActions
    from stockapp.ingest.nse_index import NseIndexClose
    from stockapp.ingest.nse_legacy import NseLegacyBhavcopy
    from stockapp.ingest.nse_mto import NseMtoDelivery
    from stockapp.ingest.nse_reference import NseEquityList, NseSectorList, NseSymbolChanges
    from stockapp.ingest.nse_udiff import NseUdiffBhavcopy

    return {
        "nse-symbol-changes": NseSymbolChanges,
        "nse-equity-list": NseEquityList,
        "nse-sector-list": NseSectorList,
        "nse-udiff": NseUdiffBhavcopy,
        "nse-legacy": NseLegacyBhavcopy,
        "nse-mto": NseMtoDelivery,
        "nse-index": NseIndexClose,
        "nse-corp-actions": NseCorporateActions,
    }


def _ingest(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.http import PoliteClient
    from stockapp.lake import Lake

    connectors = _connectors()
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


def _backfill(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.backfill import BackfillAborted, backfill
    from stockapp.ingest.http import PoliteClient
    from stockapp.lake import Lake

    lake = Lake.from_settings()
    log_dir = lake.root.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"backfill_{datetime.now(IST):%Y%m%d_%H%M%S}.log"
    end = args.end or date.today()
    print(f"Backfill {args.start}..{end}; log: {log_path}", flush=True)
    with (
        log_path.open("a", encoding="utf-8") as log_file,
        connect() as conn,
        PoliteClient(min_interval_s=args.interval) as archives,
        PoliteClient(min_interval_s=max(args.interval, 2.0)) as api,
    ):

        def log(line: str) -> None:
            print(line, flush=True)
            log_file.write(line + "\n")
            log_file.flush()

        try:
            stats = backfill(conn, lake, archives, api, args.start, end, log=log)
        except BackfillAborted as exc:
            log(f"ABORTED: {exc}")
            return 1
        log("Done.\n" + stats.summary())
        for f in stats.failures[:20]:
            log(f"  failed: {f.source_id} {f.partition_key} {f.message}")
    return 1 if stats.failures else 0


def _calendar_infer(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.calendar import infer_holidays

    with connect() as conn:
        days = infer_holidays(
            conn,
            args.start,
            args.end or date.today(),
            price_sources=("nse_legacy_bhavcopy", "nse_udiff_bhavcopy"),
        )
    print(
        f"Recorded {len(days)} inferred holiday(s)"
        + (f": {', '.join(map(str, days))}" if days else "")
    )
    return 0


def _universe_build(args: argparse.Namespace) -> int:
    from stockapp.lake import Lake
    from stockapp.universe import build_universe

    df = build_universe(Lake.from_settings(), size=get_app_config().universe.size)
    months = df["month"].n_unique() if df.height else 0
    print(f"Universe built: {months} month(s), {df.height} membership rows")
    if df.height:
        print(f"First month {df['month'].min()}, last {df['month'].max()}")
    return 0


def _coverage(args: argparse.Namespace) -> int:
    from stockapp.coverage import build_coverage
    from stockapp.db import connect
    from stockapp.lake import Lake

    lake = Lake.from_settings()
    with connect() as conn:
        result = build_coverage(conn, lake)
    reports = lake.root.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"coverage_{date.today().isoformat()}.md"
    path.write_text(result.markdown, encoding="utf-8")
    lake.write_partition("gold", "coverage_daily", "built", date.today().isoformat(), result.daily)
    print(result.markdown.split("\n## 1.")[0])
    print(f"Full report: {path}")
    return 0 if result.gate_passed else 1


def _probe_earliest(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.http import PoliteClient
    from stockapp.ingest.probe import find_earliest, record_earliest
    from stockapp.lake import Lake

    with connect() as conn, PoliteClient(min_interval_s=1.5) as http:
        connector = _connectors()[args.source](conn, Lake.from_settings(), http)
        result = find_earliest(connector, args.floor, args.known_good)
        record_earliest(conn, result)
    print(f"{result.source_id}: {result.earliest} ({result.note}) [{result.requests} requests]")
    return 0


def _quality_build(args: argparse.Namespace) -> int:
    """M3 pipeline, in dependency order. Each step rebuilds from silver/raw; nothing is edited."""
    import polars as pl

    from stockapp.adjust import build_adjustments, continuity_check
    from stockapp.lake import Lake
    from stockapp.master import build_company_master
    from stockapp.quality.gates import build_price_flags
    from stockapp.quality.score import build_quality_scores
    from stockapp.universe import build_universe

    lake, today = Lake.from_settings(), date.today()
    m = build_company_master(lake, today)
    print(f"company master: {m['company_id'].n_unique()} companies, {m.height} symbol segments")
    b = build_adjustments(lake, today)
    print(
        f"corporate actions: {b.events.height} events, {b.factors.height} factor days, "
        f"{b.breaks.height} series breaks, {b.skipped.height} skipped, {b.unmapped.height} unmapped"
    )
    c = continuity_check(lake)
    print(f"continuity: {c['passed'].sum()}/{c.height} price events reconcile")
    u = build_universe(lake, size=get_app_config().universe.size)
    print(f"universe: {u['month'].n_unique()} months, {u.height} memberships")
    f = build_price_flags(lake, today)
    counts = f.group_by("severity", "check").len().sort("severity", "check")
    print("quality flags:\n" + "\n".join(f"  {s} {k}: {n}" for s, k, n in counts.iter_rows()))
    q = build_quality_scores(lake, today)
    low = q.filter(pl.col("score") < 95).height
    print(
        f"quality score: {q.height} sessions, mean {q['score'].mean():.2f}, "
        f"min {q['score'].min():.2f}, below 95: {low}, latest {q['score'][-1]:.2f}"
    )
    from stockapp.portfolio.valuation import build_latest_prices
    from stockapp.quality.gate import evaluate_m3_gate

    lp = build_latest_prices(lake, today)
    print(f"latest prices (gold): {lp.height} companies, data as of {lp['data_as_of'].max()}")

    gate = evaluate_m3_gate(lake, q)
    print("\n".join(gate.lines))
    return 0 if gate.passed else 1


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
    ing.add_argument(
        "source",
        choices=[
            "nse-udiff", "nse-legacy", "nse-mto", "nse-index", "nse-corp-actions",
            "nse-symbol-changes", "nse-equity-list", "nse-sector-list",
        ],
    )  # fmt: skip
    ing.add_argument("--start", type=_parse_day, required=True, help="YYYY-MM-DD")
    ing.add_argument("--end", type=_parse_day, help="YYYY-MM-DD (default: same as --start)")
    ing.add_argument("--force", action="store_true", help="reload even if already loaded")
    ing.add_argument("--include-weekends", action="store_true", help="also try Sat/Sun")
    ing.add_argument("--interval", type=float, default=1.5, help="seconds between requests")
    ing.set_defaults(func=_ingest)

    bf = sub.add_parser(
        "backfill", help="resumable NSE history load (prices, delivery, indices, actions)"
    )
    bf.add_argument("--start", type=_parse_day, default=date(2016, 1, 1), help="default 2016-01-01")
    bf.add_argument("--end", type=_parse_day, help="default today")
    bf.add_argument("--interval", type=float, default=1.0, help="seconds between archive requests")
    bf.set_defaults(func=_backfill)

    uni = sub.add_parser("universe", help="point-in-time top-500 liquidity universe")
    uni.add_subparsers(dest="universe_command", required=True).add_parser(
        "build", help="rebuild monthly membership from silver prices"
    ).set_defaults(func=_universe_build)

    sub.add_parser("coverage", help="coverage report and the M2 gate").set_defaults(func=_coverage)

    qual = sub.add_parser("quality", help="M3: company master, adjustments, gates, score")
    qual.add_subparsers(dest="quality_command", required=True).add_parser(
        "build", help="rebuild master, adjustments, universe, flags and daily scores"
    ).set_defaults(func=_quality_build)

    probe = sub.add_parser("probe-earliest", help="binary-search a source's first available date")
    probe.add_argument("source", choices=["nse-udiff", "nse-legacy", "nse-mto", "nse-index"])
    probe.add_argument("--floor", type=_parse_day, required=True, help="earliest date to search")
    probe.add_argument("--known-good", type=_parse_day, required=True, help="a date with a file")
    probe.set_defaults(func=_probe_earliest)

    cal = sub.add_parser("calendar", help="NSE trading calendar")
    cal_sub = cal.add_subparsers(dest="calendar_command", required=True)
    cal_sub.add_parser("refresh", help="load NSE's current holiday list").set_defaults(
        func=_calendar_refresh
    )
    infer = cal_sub.add_parser("infer", help="record past weekdays with no price file as holidays")
    infer.add_argument("--start", type=_parse_day, default=date(2016, 1, 1))
    infer.add_argument("--end", type=_parse_day)
    infer.set_defaults(func=_calendar_infer)
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
    sub.add_parser("set-password", help="create the app login password hash").set_defaults(
        func=_set_password
    )
    sub.add_parser("config", help="print product config and environment wiring").set_defaults(
        func=_config_show
    )
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
