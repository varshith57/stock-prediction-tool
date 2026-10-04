"""Resumable history backfill for the NSE market-data sources.

Per calendar day (weekends included, to catch special sessions):
1. the price file for that day (legacy before ``UDIFF_CUTOVER``, UDiFF from it);
2. only if a price file exists: delivery (MTO) and index closes;
3. in the Jan to Jul 2024 overlap, the UDiFF file too, for the legacy/UDiFF cross-check.
Corporate actions are fetched once per month, at the first day processed in that month.

Partitions already loaded are skipped without a request, so a stopped run resumes where it left
off. The two most recent months of corporate actions are always re-fetched, since NSE keeps adding
upcoming actions to them. A circuit-open source stops the run (rerun later to resume).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg

from stockapp.ingest.base import RunResult
from stockapp.ingest.http import PoliteClient
from stockapp.ingest.nse_corp_actions import NseCorporateActions, month_start
from stockapp.ingest.nse_index import NseIndexClose
from stockapp.ingest.nse_legacy import NseLegacyBhavcopy
from stockapp.ingest.nse_mto import NseMtoDelivery
from stockapp.ingest.nse_udiff import NseUdiffBhavcopy
from stockapp.ingest.prices import UDIFF_CUTOVER
from stockapp.lake import Lake

OVERLAP_START = date(2024, 1, 1)
LOADED = ("success", "skipped")


class BackfillAborted(RuntimeError):
    pass


@dataclass
class BackfillStats:
    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    failures: list[RunResult] = field(default_factory=list)

    def add(self, r: RunResult) -> RunResult:
        self.counts[(r.source_id, r.status)] += 1
        if r.status == "failed":
            self.failures.append(r)
        return r

    def summary(self) -> str:
        by_source: dict[str, dict[str, int]] = {}
        for (src, status), n in sorted(self.counts.items()):
            by_source.setdefault(src, {})[status] = n
        return "\n".join(f"  {src}: {stats}" for src, stats in by_source.items())


def backfill(
    conn: psycopg.Connection,
    lake: Lake,
    archives: PoliteClient,
    api: PoliteClient,
    start: date,
    end: date,
    *,
    today: date | None = None,
    log: Callable[[str], None] = print,
) -> BackfillStats:
    today = today or date.today()
    legacy = NseLegacyBhavcopy(conn, lake, archives)
    udiff = NseUdiffBhavcopy(conn, lake, archives)
    mto = NseMtoDelivery(conn, lake, archives)
    idx = NseIndexClose(conn, lake, archives)
    actions = NseCorporateActions(conn, lake, api)
    refresh_from = month_start(month_start(today) - timedelta(days=1))  # last month and this one

    stats = BackfillStats()
    total_days = (end - start).days + 1
    started = time.monotonic()
    last_month: date | None = None
    day = start
    while day <= end:
        month = month_start(day)
        if month != last_month:
            last_month = month
            stats.add(actions.run(month, skip_if_loaded=month < refresh_from))

        price = legacy if day < UDIFF_CUTOVER else udiff
        r = stats.add(price.run(day, skip_if_loaded=True))
        line = [f"{day} {day:%a} price={r.status}"]
        if r.status in LOADED:
            line.append(f"mto={stats.add(mto.run(day, skip_if_loaded=True)).status}")
            line.append(f"idx={stats.add(idx.run(day, skip_if_loaded=True)).status}")
            if OVERLAP_START <= day < UDIFF_CUTOVER:
                line.append(f"udiff={stats.add(udiff.run(day, skip_if_loaded=True)).status}")
        elif r.status == "failed":
            line.append(r.message)

        done = (day - start).days + 1
        rate = (time.monotonic() - started) / done
        line.append(
            f"[{done}/{total_days}, eta {timedelta(seconds=int(rate * (total_days - done)))}]"
        )
        log(" ".join(line))

        for client, name in ((archives, "nsearchives"), (api, "nse api")):
            if client.consecutive_failures >= client.breaker_threshold:
                raise BackfillAborted(
                    f"{name} failed {client.consecutive_failures} times in a row at {day}; "
                    "stopped. Rerun the same command later to resume."
                )
        day += timedelta(days=1)
    return stats
