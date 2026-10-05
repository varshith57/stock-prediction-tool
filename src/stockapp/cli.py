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


def _features_build(_: argparse.Namespace) -> int:
    import polars as pl

    from stockapp.features.pipeline import FEATURE_VERSION, build_weekly_samples
    from stockapp.lake import Lake

    s = build_weekly_samples(Lake.from_settings(), get_app_config(), date.today())
    labelled = s.filter(pl.col("label_a").is_not_null() & ~pl.col("blocked"))
    print(
        f"weekly samples: {s.height} rows, {s['trade_date'].n_unique()} weeks "
        f"({s['trade_date'].min()} to {s['trade_date'].max()}), feature version {FEATURE_VERSION}"
    )
    print(
        f"labelled, unblocked: {labelled.height}; blocked: {s['blocked'].sum()}; "
        f"base rate A {labelled['label_a'].mean():.2%}, C {labelled['label_c'].mean():.2%}"
    )
    return 0


def _features_check(args: argparse.Namespace) -> int:
    """Leakage gate on real data: shuffled labels must give AUC about 0.5."""
    import polars as pl

    from stockapp.features.build import FEATURE_COLUMNS
    from stockapp.features.checks import holdout_auc, shuffled_label_auc
    from stockapp.features.pipeline import load_weekly_samples
    from stockapp.lake import Lake

    s = load_weekly_samples(Lake.from_settings()).filter(~pl.col("blocked"))
    ok = True
    for label in ("label_a", "label_c"):
        real = holdout_auc(s, FEATURE_COLUMNS, label, args.split)
        shuffled = shuffled_label_auc(s, FEATURE_COLUMNS, label, args.split)
        passed = abs(shuffled - 0.5) <= 0.02
        ok &= passed
        print(
            f"{label}: holdout AUC {real:.3f} (logistic baseline); shuffled-label AUC "
            f"{shuffled:.3f} {'OK' if passed else 'FAIL: features encode something besides signal'}"
        )
    print(f"M5 leakage gate: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _models_backtest(_: argparse.Namespace) -> int:
    from stockapp.lake import Lake
    from stockapp.models.backtest import summarize
    from stockapp.models.run import run_backtests

    run = run_backtests(Lake.from_settings(), get_app_config(), date.today())
    for signal, bt in run.backtests.items():
        t = {r["score"]: r for r in summarize(bt).iter_rows(named=True)}
        auc = {k: t[k]["auc"] for k in ("model", "volatility", "logistic")}
        top5 = {k: t[k]["top5_precision"] for k in ("model", "volatility", "base_rate")}
        print(
            f"signal {signal}: model AUC {auc['model']:.3f} (volatility {auc['volatility']:.3f}, "
            f"logistic {auc['logistic']:.3f}); top-5 precision {top5['model']:.1%} "
            f"(volatility {top5['volatility']:.1%}, base {top5['base_rate']:.1%})"
        )
        g = run.gates[signal]
        print(f"  gate {g.status}: {g.reason}")
    print(f"report: {run.report_path}")
    return 0


def _models_compare(args: argparse.Namespace) -> int:
    from stockapp.lake import Lake
    from stockapp.models.compare import compare_models

    run = compare_models(
        Lake.from_settings(), get_app_config(), date.today(), args.gain, args.crash, args.window
    )
    for r in run.table.iter_rows(named=True):
        best = "-" if r["best_precision"] is None else f"{r['best_precision']:.1%}"
        print(
            f"{r['signal']}  {r['model']:<14} AUC {r['auc']:.3f}  top-5 {r['top5_precision']:.1%}"
            f"  best {best} ({r['signals']} signals)  {r['gate']}"
        )
    print(f"report: {run.report_path}")
    return 0


def _models_money(_: argparse.Namespace) -> int:
    from stockapp.lake import Lake
    from stockapp.models.money import evaluate_money_gate, run_money_backtest

    lake, cfg = Lake.from_settings(), get_app_config()
    run = run_money_backtest(lake, cfg, date.today())
    for r in run.table.iter_rows(named=True):
        x = "-" if r["xirr"] is None else f"{r['xirr']:+.1%}"
        print(
            f"{r['scenario'][:24]:<24} {r['strategy']:<28} end Rs {r['final_value']:>12,.0f}  "
            f"in Rs {r['contributed']:>10,.0f}  XIRR {x:>7}  worst {r['max_drawdown']:+.1%}"
        )
    print(f"report: {run.report_path}")
    gate = evaluate_money_gate(lake, cfg, date.today())
    print(f"money test for buy ideas: {gate.status}: {gate.reason}")
    return 0


INTEGRATED_FROM = date(2024, 10, 1)  # integrated filings start early 2025; overlap for checks


def _results_backfill(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.ingest.http import PoliteClient
    from stockapp.ingest.nse_corp_actions import month_start
    from stockapp.ingest.nse_results import (
        NseBoardMeetings,
        NseFinancialResults,
        NseIntegratedResults,
    )
    from stockapp.lake import Lake

    lake, end = Lake.from_settings(), args.end or date.today()
    failed = 0
    with connect() as conn, PoliteClient(min_interval_s=args.interval) as http:
        http.get("https://www.nseindia.com/companies-listing/corporate-filings-board-meetings")
        month = month_start(args.start)
        while month <= month_start(end):
            sources = [NseBoardMeetings, NseFinancialResults]
            if month >= INTEGRATED_FROM:
                sources.append(NseIntegratedResults)
            for c in sources:
                r = c(conn, lake, http).run(month, skip_if_loaded=not args.force)
                print(f"{c.source_id} {r.partition_key} {r.status} rows={r.rows or 0}", flush=True)
                failed += r.status == "failed"
            month = month_start(month + timedelta(days=32))
    return 1 if failed else 0


def _fundamentals_backfill(args: argparse.Namespace) -> int:
    from stockapp.db import connect
    from stockapp.fundamentals import backfill
    from stockapp.lake import Lake

    def log(msg: str) -> None:
        print(f"[{datetime.now():%H:%M}] {msg}", flush=True)

    with connect() as conn:
        backfill(conn, Lake.from_settings(), interval=args.interval, limit=args.limit, log=log)
    return 0


def _safety_build(_: argparse.Namespace) -> int:
    from datetime import datetime

    from stockapp.lake import Lake
    from stockapp.models.safety import build_safety

    def progress(i, n, fold):
        print(f"[{datetime.now():%H:%M}] quarter {i}/{n} ({fold.test_start})", flush=True)

    gate = build_safety(Lake.from_settings(), get_app_config(), date.today(), on_fold=progress)
    print(f"safety net {gate.status}: {gate.reason}")
    return 0


def _models_train(_: argparse.Namespace) -> int:
    from stockapp.lake import Lake
    from stockapp.models.run import train_and_score

    scores = train_and_score(Lake.from_settings(), get_app_config(), date.today())
    print(
        f"scored {scores.height} universe members for week ending {scores['trade_date'][0]} "
        f"(model {scores['model_version'][0]})"
    )
    return 0


def plan_summary(plan) -> str:
    """Telegram text: counts, tickers and status only (never amounts or holdings values)."""
    if plan.status == "NO_SIGNAL":
        return f"Weekly plan for {plan.week_of:%a %d %b}: NO SIGNAL ({plan.status_reason})."
    parts = [f"Weekly plan for {plan.week_of:%a %d %b}: {plan.action_count} action(s)."]
    if plan.exits:
        parts.append("Sell: " + ", ".join(i.symbol for i in plan.exits) + ".")
    if plan.opportunities:
        parts.append("Buy: " + ", ".join(i.symbol for i in plan.opportunities) + ".")
    off = [s for s, g in plan.gates.items() if g.status != "LIVE"]
    if off:
        parts.append(f"Signal(s) {', '.join(off)} OFF (can't meet the 90% bar yet).")
    if plan.closest:
        parts.append(f"Closest: {plan.closest['symbol']} at {plan.closest['probability']:.0%}.")
    parts.extend(n[0].upper() + n[1:] + "." for n in plan.notes if "paused" in n or "stress" in n)
    return " ".join(parts)


def _plan_build(args: argparse.Namespace) -> int:
    from stockapp import pipeline
    from stockapp.db import connect
    from stockapp.lake import Lake

    lake, cfg, today = Lake.from_settings(), get_app_config(), date.today()
    with connect() as conn:
        plan, plan_id = pipeline.build_weekly_plan(conn, lake, cfg, today)
    text = plan_summary(plan)
    print(f"plan {plan_id}: {text}")
    if args.notify:
        try:
            send_message(text)
        except AlertError as exc:
            print(f"Telegram failed: {exc}", file=sys.stderr)
            return 1
    return 0


def _job(args: argparse.Namespace) -> int:
    from stockapp import jobs
    from stockapp.db import connect, migrate
    from stockapp.lake import Lake

    today = args.date or date.today()
    if args.name == "monthly" and args.date is None and today.day > 7:
        print("monthly: not the first Saturday of the month; nothing to do")
        return 0
    cfg, lake = get_app_config(), Lake.from_settings()
    print(f"[{datetime.now(IST):%Y-%m-%d %H:%M}] job {args.name} for {today}", flush=True)
    try:
        conn_cm = connect()
        conn = conn_cm.__enter__()
    except Exception as exc:
        import contextlib

        with contextlib.suppress(AlertError):
            send_message(
                f"stockapp {args.name} job couldn't reach its database "
                f"({type(exc).__name__}). Is Docker running?"
            )
        print(f"FAILED: database unreachable: {exc}", file=sys.stderr)
        return 1
    try:
        migrate(conn)
        steps = {
            "daily": jobs.daily_steps,
            "weekly": jobs.weekly_steps,
            "monthly": jobs.monthly_steps,
        }[args.name](conn, lake, cfg, today)
        result = jobs.run_steps(conn, cfg, args.name, steps, today)
    finally:
        conn_cm.__exit__(None, None, None)
    print("\n".join(result.lines), flush=True)
    return 0 if result.ok else 1


def _schedule(args: argparse.Namespace) -> int:
    from stockapp import schedule

    if args.action == "show":
        for job in schedule.TIMES:
            print(
                f"# {schedule.AGENTS}/{schedule.LABEL.format(job=job)}.plist\n{schedule.plist(job)}"
            )
    elif args.action == "install":
        print("Installed and loaded:\n" + "\n".join(schedule.install()))
    else:
        print("Removed:\n" + "\n".join(schedule.uninstall() or ["(nothing installed)"]))
    return 0


def _retrain(_: argparse.Namespace) -> int:
    """After a threshold/window/universe change: rebuild labels and features, re-run the
    walk-forward backtest and gate, retrain, and rebuild this week's plan."""
    from stockapp import pipeline
    from stockapp.db import connect
    from stockapp.features.pipeline import build_weekly_samples
    from stockapp.lake import Lake
    from stockapp.models.money import evaluate_money_gate
    from stockapp.models.run import run_backtests, train_and_score

    lake, cfg, today = Lake.from_settings(), get_app_config(), date.today()
    steps = [
        ("universe and quality", lambda: pipeline.rebuild_quality(lake, cfg, today)),
        ("features and labels", lambda: f"{build_weekly_samples(lake, cfg, today).height} samples"),
        (
            "walk-forward backtest and gate",
            lambda: ", ".join(
                f"{s} {g.status}" for s, g in run_backtests(lake, cfg, today).gates.items()
            ),
        ),
        (
            "money test (beat the index after costs?)",
            lambda: evaluate_money_gate(lake, cfg, today).status,
        ),
        ("final models", lambda: f"{train_and_score(lake, cfg, today).height} stocks scored"),
    ]
    try:
        for i, (name, fn) in enumerate(steps, 1):
            print(f"[{i}/{len(steps) + 1}] {name}...", flush=True)
            print(f"      {fn()}", flush=True)
        print(f"[{len(steps) + 1}/{len(steps) + 1}] weekly plan...", flush=True)
        with connect() as conn:
            plan, _ = pipeline.build_weekly_plan(conn, lake, cfg, today)
        print(f"      {plan.action_count} action(s)", flush=True)
    except Exception as exc:
        print(f"RETRAIN FAILED: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print("RETRAIN DONE", flush=True)
    return 0


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
            "nse-udiff",
            "nse-legacy",
            "nse-mto",
            "nse-index",
            "nse-corp-actions",
            "nse-symbol-changes",
            "nse-equity-list",
            "nse-sector-list",
        ],
    )
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

    mod = sub.add_parser("models", help="M6-M7: walk-forward backtest, gate, final models")
    mod_sub = mod.add_subparsers(dest="models_command", required=True)
    mod_sub.add_parser(
        "backtest", help="walk-forward A and C, baselines, gate, report"
    ).set_defaults(func=_models_backtest)
    mc = mod_sub.add_parser(
        "compare", help="group of 5 models vs each member, same walk-forward test (no changes)"
    )
    mc.add_argument("--gain", type=float, help="test another rise, e.g. 0.05 (labels in memory)")
    mc.add_argument("--crash", type=float, help="drop to test (default: same as --gain)")
    mc.add_argument("--window", type=int, help="market days for the move, e.g. 20")
    mc.set_defaults(func=_models_compare)
    mod_sub.add_parser(
        "money", help="would following the app have made money? costs, tax, vs the index"
    ).set_defaults(func=_models_money)
    mod_sub.add_parser("train", help="fit final models and score the latest week").set_defaults(
        func=_models_train
    )

    rs = sub.add_parser("results", help="results dates: board meetings and results filings")
    rb = rs.add_subparsers(dest="results_command", required=True).add_parser(
        "backfill", help="fetch month by month (resumable; loaded months are skipped)"
    )
    rb.add_argument("--start", type=_parse_day, default=date(2016, 1, 1))
    rb.add_argument("--end", type=_parse_day)
    rb.add_argument("--force", action="store_true", help="refetch months already loaded")
    rb.add_argument("--interval", type=float, default=1.5)
    rb.set_defaults(func=_results_backfill)

    fu = sub.add_parser("fundamentals", help="quarterly results figures from XBRL filings")
    fb = fu.add_subparsers(dest="fundamentals_command", required=True).add_parser(
        "backfill", help="download and parse (resumable; ~40k files, ~11 h at 1/s)"
    )
    fb.add_argument("--limit", type=int, help="stop after this many files (for a trial)")
    fb.add_argument("--interval", type=float, default=1.0, help="seconds between requests")
    fb.set_defaults(func=_fundamentals_backfill)

    sf = sub.add_parser("safety", help="monthly drop warnings for holdings")
    sf.add_subparsers(dest="safety_command", required=True).add_parser(
        "build", help="test, train and score the safety net (~40 min with the group of models)"
    ).set_defaults(func=_safety_build)

    pl_ = sub.add_parser("plan", help="M8: weekly plan")
    pb = pl_.add_subparsers(dest="plan_command", required=True).add_parser(
        "build", help="build this week's plan from the latest scores, gate, holdings and rules"
    )
    pb.add_argument("--notify", action="store_true", help="send the Telegram summary")
    pb.set_defaults(func=_plan_build)

    job = sub.add_parser("job", help="M9: run a scheduled job now")
    job.add_argument("name", choices=["daily", "weekly", "monthly"])
    job.add_argument("--date", type=_parse_day, help="run as if on this date (default today)")
    job.set_defaults(func=_job)
    sub.add_parser(
        "retrain", help="rebuild labels, backtest, gate and models after a change"
    ).set_defaults(func=_retrain)
    sch = sub.add_parser("schedule", help="M9: launchd schedule on this Mac")
    sch.add_argument("action", choices=["show", "install", "uninstall"])
    sch.set_defaults(func=_schedule)

    feat = sub.add_parser("features", help="M5: point-in-time features and labels")
    feat_sub = feat.add_subparsers(dest="features_command", required=True)
    feat_sub.add_parser(
        "build", help="rebuild the weekly sample set (features, labels A/C, blocked flags)"
    ).set_defaults(func=_features_build)
    chk = feat_sub.add_parser("check", help="leakage gate: shuffled labels must give AUC ~0.5")
    chk.add_argument("--split", type=_parse_day, default=date(2022, 1, 1), help="holdout start")
    chk.set_defaults(func=_features_check)

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
