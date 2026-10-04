import httpx
import pytest
import respx

from stockapp.ingest.http import Blocked, CircuitOpen, FetchError, NotAvailable, PoliteClient

URL = "https://example.test/file.zip"


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def t() -> FakeTime:
    return FakeTime()


def client(t: FakeTime, **kw) -> PoliteClient:
    return PoliteClient(sleep=t.sleep, clock=t.clock, **kw)


@respx.mock
def test_success_sends_browser_headers(t: FakeTime):
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=b"ok"))
    assert client(t).get(URL).content == b"ok"
    assert "Mozilla" in route.calls.last.request.headers["user-agent"]


@respx.mock
def test_404_is_not_available_and_not_retried(t: FakeTime):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    c = client(t)
    with pytest.raises(NotAvailable):
        c.get(URL)
    assert route.call_count == 1
    assert c.consecutive_failures == 0


@respx.mock
def test_retries_server_errors_with_backoff(t: FakeTime):
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    client(t, min_interval_s=0, backoff_s=2).get(URL)
    assert route.call_count == 2
    assert t.sleeps == [2]


@respx.mock
def test_gives_up_after_retries(t: FakeTime):
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    c = client(t, min_interval_s=0, retries=2, backoff_s=1)
    with pytest.raises(FetchError, match="ConnectError"):
        c.get(URL)
    assert route.call_count == 3
    assert t.sleeps == [1, 2]
    assert c.consecutive_failures == 1


@respx.mock
def test_403_is_blocked_without_retry(t: FakeTime):
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(Blocked):
        client(t).get(URL)
    assert route.call_count == 1


@respx.mock
def test_circuit_opens_after_threshold(t: FakeTime):
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    c = client(t, breaker_threshold=2)
    for _ in range(2):
        with pytest.raises(Blocked):
            c.get(URL)
    with pytest.raises(CircuitOpen):
        c.get(URL)
    assert route.call_count == 2


@respx.mock
def test_rate_limit_spaces_requests(t: FakeTime):
    respx.get(URL).mock(return_value=httpx.Response(200))
    c = client(t, min_interval_s=1.5)
    c.get(URL)
    t.now += 0.5
    c.get(URL)
    assert t.sleeps == [1.0]
