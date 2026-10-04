from datetime import date, timedelta
from typing import ClassVar

import pytest

from stockapp.ingest.http import NotAvailable
from stockapp.ingest.probe import find_earliest


class FakeHttp:
    def __init__(self, start: date, holidays: set[date]):
        self.start, self.holidays, self.calls = start, holidays, 0

    def get(self, url: str, headers=None):
        self.calls += 1
        day = date.fromisoformat(url)
        if day < self.start or day in self.holidays:
            raise NotAvailable(url)
        return object()


class FakeConnector:
    source_id = "fake"
    request_headers: ClassVar[dict[str, str]] = {}

    def __init__(self, http: FakeHttp):
        self.http = http

    def url_for(self, day: date) -> str:
        return day.isoformat()


@pytest.mark.parametrize(
    "start",
    [date(2024, 1, 2), date(2023, 7, 3), date(2023, 2, 6), date(2023, 12, 29)],
)
def test_finds_true_start_despite_holidays(start: date):
    # holidays right around the midpoints and just after the start must not mislead the search
    holidays = {start + timedelta(days=d) for d in (1, 2, 3)} | {date(2023, 7, 3), date(2023, 7, 4)}
    holidays.discard(start)
    http = FakeHttp(start, holidays)
    result = find_earliest(
        FakeConnector(http), date(2023, 1, 2), date(2024, 3, 1), log=lambda _: None
    )
    assert result.earliest == start
    assert not result.at_floor
    assert http.calls < 120  # bounded, no loop


def test_reports_floor_when_floor_has_files():
    http = FakeHttp(date(2000, 1, 3), set())
    result = find_earliest(
        FakeConnector(http), date(2010, 1, 4), date(2016, 1, 4), log=lambda _: None
    )
    assert (result.earliest, result.at_floor) == (date(2010, 1, 4), True)
    assert "search floor" in result.note


def test_bad_known_good_raises():
    http = FakeHttp(date(2030, 1, 1), set())
    with pytest.raises(RuntimeError, match="no file found"):
        find_earliest(FakeConnector(http), date(2023, 1, 2), date(2024, 1, 2), log=lambda _: None)
