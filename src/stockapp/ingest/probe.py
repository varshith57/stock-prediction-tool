"""Find a daily source's earliest available date and record it in the source registry.

Binary search between a floor and a date known to exist, assuming that once a source starts
publishing it keeps publishing. A probe looks at up to ``SPAN`` consecutive weekdays from the
midpoint, so a holiday 404 isn't read as "not published yet". If the floor itself has a file, the
result is reported as "available at the search floor" (not a true start date).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg

from stockapp.ingest.base import DailyFileConnector
from stockapp.ingest.http import NotAvailable

SPAN = 6  # weekdays checked per probe: longer than any NSE holiday run


@dataclass(frozen=True)
class EarliestResult:
    source_id: str
    earliest: date
    at_floor: bool
    requests: int

    @property
    def note(self) -> str:
        if self.at_floor:
            return (
                f"At the search floor: first file {self.earliest}, within {SPAN} weekdays "
                "of the floor; earlier dates not searched."
            )
        return f"First file found by binary search: {self.earliest}."


def _weekdays_from(day: date, n: int) -> list[date]:
    out, d = [], day
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def find_earliest(
    connector: DailyFileConnector,
    floor: date,
    known_good: date,
    *,
    log: Callable[[str], None] = print,
) -> EarliestResult:
    requests = 0

    def first_available_near(day: date) -> date | None:
        nonlocal requests
        for d in _weekdays_from(day, SPAN):
            requests += 1
            try:
                connector.http.get(connector.url_for(d), headers=connector.request_headers or None)
            except NotAvailable:
                continue
            log(f"  {connector.source_id} {d}: file exists")
            return d
        log(f"  {connector.source_id} {day}..: no file in {SPAN} weekdays")
        return None

    hit = first_available_near(floor)
    if hit is not None:  # a file within SPAN weekdays of the floor: report the floor, don't search
        return EarliestResult(connector.source_id, hit, True, requests)
    # Invariant: no file in the SPAN weekdays from lo; a file within SPAN weekdays from hi.
    # Each step moves one bound to the midpoint, so the interval always halves.
    lo, hi = floor, known_good
    while (hi - lo).days > 2 * SPAN:
        mid = lo + timedelta(days=(hi - lo).days // 2)
        if first_available_near(mid) is None:
            lo = mid
        else:
            hi = mid
    # finish with a linear scan over the remaining few weeks
    d, stop = lo, hi + timedelta(days=2 * SPAN)
    while d <= stop:
        if d.weekday() < 5:
            requests += 1
            try:
                connector.http.get(connector.url_for(d), headers=connector.request_headers or None)
            except NotAvailable:
                d += timedelta(days=1)
                continue
            return EarliestResult(connector.source_id, d, False, requests)
        d += timedelta(days=1)
    raise RuntimeError(
        f"{connector.source_id}: no file found up to {stop}; is {known_good} really available?"
    )


def record_earliest(conn: psycopg.Connection, result: EarliestResult, extra_note: str = "") -> None:
    note = f"{result.note} {extra_note}".strip()
    conn.execute(
        """UPDATE source_registry
           SET earliest_date = %s, earliest_date_note = %s, updated_at = now()
           WHERE source_id = %s""",
        (result.earliest, note, result.source_id),
    )
