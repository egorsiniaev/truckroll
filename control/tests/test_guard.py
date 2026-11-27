import httpx
from starlette.requests import Request
from test_app import SITES, FakeCsms, FakeSim, FakeToxi

from control.app import Clients, Hub, create_app
from control.guard import Guard, Limits, client_ip


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def request(headers=None, host="10.0.0.1"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "headers": raw, "client": (host, 1234)})


def test_client_ip_takes_the_last_forwarded_entry():
    assert client_ip(request({"X-Forwarded-For": "6.6.6.6, 1.2.3.4"})) == "1.2.3.4"
    assert client_ip(request()) == "10.0.0.1"


def test_writes_are_limited_per_ip_and_recover_after_the_window():
    clock = Clock()
    guard = Guard(Limits(writes_per_ip=2, writes_total=100), clock)
    assert [guard.allow_request("POST", "a") for _ in range(3)] == [True, True, False]
    assert guard.allow_request("POST", "b")
    clock.t = 60
    assert guard.allow_request("POST", "a")


def test_writes_are_limited_across_all_visitors():
    guard = Guard(Limits(writes_per_ip=10, writes_total=3), Clock())
    assert [guard.allow_request("POST", ip) for ip in "abcd"] == [True, True, True, False]


def test_reads_have_their_own_budget():
    guard = Guard(Limits(writes_per_ip=1, reads_per_ip=2), Clock())
    assert guard.allow_request("POST", "a")
    assert [guard.allow_request("GET", "a") for _ in range(3)] == [True, True, False]


def test_live_views_are_capped_per_ip_and_in_total():
    guard = Guard(Limits(streams_per_ip=2, streams_total=3), Clock())
    assert [guard.open_stream("a") for _ in range(3)] == [True, True, False]
    assert guard.open_stream("b")
    assert not guard.open_stream("c")
    guard.close_stream("a")
    assert guard.open_stream("c")


async def test_app_answers_429_once_an_ip_is_over_its_write_budget():
    clients = Clients(csms=FakeCsms(), toxiproxy=FakeToxi(), simulator=FakeSim(), weather=None)
    hub = Hub(sites=SITES, clients=clients, ledger_path=None)
    await hub.poll_once()
    app = create_app(hub, web_dir=None, start_poller=False, guard=Guard(Limits(writes_per_ip=1), Clock()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        assert (await client.post("/api/chaos/clear")).status_code == 200
        res = await client.post("/api/chaos/clear")
        assert res.status_code == 429
        assert res.headers["retry-after"] == "60"
        assert (await client.get("/api/healthz")).status_code == 200
