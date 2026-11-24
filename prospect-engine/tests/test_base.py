"""Tests for src/providers/base.py using a fake network (httpx.MockTransport)."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src import db
from src.providers import base
from src.providers.base import (
    BudgetExceeded,
    CallContext,
    CostEstimate,
    HttpRequest,
    MissingApiKey,
    MockFixtureMissing,
    ProviderError,
    ProviderHTTPError,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SECRET = "sk-test-not-a-real-key"

SETTINGS = {
    "budget": {"run_default_usd": 1.00, "monthly_cap_usd": 2.00},
    "http": {"timeout_seconds": 5, "max_retries": 4, "backoff_base_seconds": 2},
    "cache_default_days": 30,
    "providers": {
        "paidco": {"plan": "paid", "cache_days": {"search": 7, "default": 90}},
        "freeco": {"plan": "free", "monthly_credit_caps": {"api": 10, "default": 5}},
    },
}


class FakeServer:
    """Returns queued responses and counts how many requests it received."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        # fresh copy each time: a Response object can't be read twice
        return httpx.Response(item.status_code, content=item.content, headers=item.headers)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def make_ctx(conn, server: FakeServer, **kw) -> tuple[CallContext, list[float]]:
    sleeps: list[float] = []
    ctx = CallContext(conn=conn, settings=SETTINGS, run_id="run-1",
                      http=httpx.Client(transport=httpx.MockTransport(server)),
                      sleep=sleeps.append, now=lambda: NOW, **kw)
    return ctx, sleeps


def req(provider="paidco", endpoint="search", **kw) -> HttpRequest:
    return HttpRequest(provider=provider, endpoint=endpoint, method="GET",
                       url="https://api.paidco.example/v1/search",
                       params={"q": "acme"}, headers={"Authorization": f"Bearer {SECRET}"}, **kw)


def calls(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM api_calls ORDER BY id")]


# ---------- helpers ----------

def test_params_hash_ignores_order_and_none():
    a = base.params_hash("p", "e", {"a": 1, "b": 2, "c": None})
    b = base.params_hash("p", "e", {"b": 2, "a": 1})
    assert a == b
    assert a != base.params_hash("p", "e", {"a": 1, "b": 3})
    assert a != base.params_hash("p", "other", {"a": 1, "b": 2})


def test_cache_days_lookup_order():
    assert base.cache_days(SETTINGS, "paidco", "search") == 7
    assert base.cache_days(SETTINGS, "paidco", "enrich") == 90
    assert base.cache_days(SETTINGS, "unknownco", "x") == 30


def test_require_key_names_missing_variable(monkeypatch):
    monkeypatch.delenv("FAKE_PROVIDER_KEY", raising=False)
    with pytest.raises(MissingApiKey, match="FAKE_PROVIDER_KEY"):
        base.require_key("FAKE_PROVIDER_KEY")
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "abc")
    assert base.require_key("FAKE_PROVIDER_KEY") == "abc"


# ---------- success, cache, logging ----------

def test_success_is_logged_and_cached_then_second_call_is_free(conn):
    server = FakeServer(httpx.Response(200, json={"results": [1, 2]}))
    ctx, _ = make_ctx(conn, server)

    first = base.call_json(ctx, req(), CostEstimate(usd=0.10))
    second = base.call_json(ctx, req(), CostEstimate(usd=0.10))

    assert first == second == {"results": [1, 2]}
    assert len(server.requests) == 1                       # second came from cache
    rows = calls(conn)
    assert [r["cached"] for r in rows] == [0, 1]
    assert rows[0]["est_cost_usd"] == pytest.approx(0.10)
    assert rows[1]["est_cost_usd"] == 0
    assert server.requests[0].headers["user-agent"] == base.USER_AGENT


def test_api_key_never_stored(conn):
    server = FakeServer(httpx.Response(200, json={"ok": True}))
    ctx, _ = make_ctx(conn, server)
    base.call_json(ctx, req(), CostEstimate())
    dump = "\n".join(conn.iterdump())
    assert SECRET not in dump


def test_cache_expires_per_provider_setting(conn):
    server = FakeServer(httpx.Response(200, json={"v": 1}))
    ctx, _ = make_ctx(conn, server)
    base.call_json(ctx, req(), CostEstimate())
    ctx.now = lambda: NOW + timedelta(days=8)               # search cache_days = 7
    base.call_json(ctx, req(), CostEstimate())
    assert len(server.requests) == 2


def test_accepted_404_is_returned_and_cached(conn):
    server = FakeServer(httpx.Response(404, json={"error": "no match"}))
    ctx, _ = make_ctx(conn, server)
    r = req(accept_status=frozenset({200, 404}))
    out = base.call_json(ctx, r, CostEstimate())
    assert out == {"_http_status": 404, "body": {"error": "no match"}}
    base.call_json(ctx, r, CostEstimate())
    assert len(server.requests) == 1


# ---------- retries and failures ----------

def test_retries_429_then_succeeds(conn):
    server = FakeServer(httpx.Response(429), httpx.Response(200, json={"ok": True}))
    ctx, sleeps = make_ctx(conn, server)
    assert base.call_json(ctx, req(), CostEstimate()) == {"ok": True}
    assert len(server.requests) == 2
    assert sleeps == [2]


def test_gives_up_after_max_retries_and_logs_error(conn):
    server = FakeServer(httpx.Response(503))
    ctx, sleeps = make_ctx(conn, server)
    with pytest.raises(ProviderError):
        base.call_json(ctx, req(), CostEstimate(usd=0.10))
    assert len(server.requests) == 5                        # 1 try + 4 retries
    assert sleeps == [2, 4, 8, 16]
    row = calls(conn)[-1]
    assert row["status_code"] == 503 and "503" in row["error"] and row["est_cost_usd"] == 0
    assert db.cache_get(conn, "paidco", "search", row["params_hash"], 90, now=NOW) is None


def test_timeout_is_retried(conn):
    server = FakeServer(httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": True}))
    ctx, sleeps = make_ctx(conn, server)
    assert base.call_json(ctx, req(), CostEstimate()) == {"ok": True}
    assert sleeps == [2]


def test_401_is_not_retried(conn):
    server = FakeServer(httpx.Response(401, json={"error": "bad key"}))
    ctx, sleeps = make_ctx(conn, server)
    with pytest.raises(ProviderHTTPError) as info:
        base.call_json(ctx, req(), CostEstimate())
    assert info.value.status_code == 401
    assert len(server.requests) == 1 and sleeps == []
    assert calls(conn)[-1]["status_code"] == 401


# ---------- budget guard ----------

def test_run_budget_refuses_before_sending(conn):
    server = FakeServer(httpx.Response(200, json={"ok": True}))
    ctx, _ = make_ctx(conn, server, budget_usd=0.15)
    base.call_json(ctx, req(), CostEstimate(usd=0.10))
    other = req()
    other.params = {"q": "different"}
    with pytest.raises(BudgetExceeded, match="run budget"):
        base.call_json(ctx, other, CostEstimate(usd=0.10))
    assert len(server.requests) == 1
    assert calls(conn)[-1]["error"].startswith("refused: run budget")


def test_monthly_cap_refuses(conn):
    db.log_api_call(conn, run_id="earlier", provider="paidco", endpoint="search",
                    params_hash="x", status_code=200, est_cost_usd=1.95, now=NOW)
    server = FakeServer(httpx.Response(200, json={"ok": True}))
    ctx, _ = make_ctx(conn, server)
    with pytest.raises(BudgetExceeded, match="monthly cap"):
        base.call_json(ctx, req(), CostEstimate(usd=0.10))
    assert server.requests == []


def test_free_plan_logs_zero_dollars_and_caps_credits(conn):
    server = FakeServer(httpx.Response(200, json={"ok": True}))
    ctx, _ = make_ctx(conn, server)
    for i in range(2):
        r = req(provider="freeco")
        r.params = {"q": f"page{i}"}
        base.call_json(ctx, r, CostEstimate(usd=0.50, credits=4, credit_bucket="api"))
    assert all(row["est_cost_usd"] == 0 for row in calls(conn))
    r = req(provider="freeco")
    r.params = {"q": "page2"}
    with pytest.raises(BudgetExceeded, match="credits"):
        base.call_json(ctx, r, CostEstimate(usd=0.50, credits=4, credit_bucket="api"))
    assert len(server.requests) == 2


def test_actual_cost_replaces_estimate(conn):
    server = FakeServer(httpx.Response(200, json={"found": False}))
    ctx, _ = make_ctx(conn, server)
    base.call_json(ctx, req(), CostEstimate(usd=0.10),
                   actual=lambda data: CostEstimate(usd=0.0 if not data["found"] else 0.10))
    assert calls(conn)[-1]["est_cost_usd"] == 0


# ---------- throttle and interpret ----------

def test_throttle_waits_between_calls(conn):
    settings = {**SETTINGS, "providers": {**SETTINGS["providers"],
                "paidco": {"plan": "paid", "min_interval_seconds": {"search": 1.0}}}}
    server = FakeServer(httpx.Response(200, json={"ok": True}))
    clock = iter([100.0, 100.3, 101.0])   # 1st call at 100.0; 2nd checks at 100.3 -> wait 0.7
    ctx, sleeps = make_ctx(conn, server)
    ctx.settings, ctx.clock = settings, lambda: next(clock)
    base.call_json(ctx, req(), CostEstimate())
    second = req()
    second.params = {"q": "other"}
    base.call_json(ctx, second, CostEstimate())
    assert sleeps == [pytest.approx(0.7)]


def test_interpret_can_turn_error_body_into_cached_empty_result(conn):
    server = FakeServer(httpx.Response(400, json={"error": True, "error_code": "NO_MATCH"}))
    ctx, _ = make_ctx(conn, server)

    def interpret(status, body):
        if body.get("error_code") == "NO_MATCH":
            return {"results": []}
        raise ProviderHTTPError("bad", status)

    r = req(accept_status=frozenset({200, 400}), interpret=interpret)
    assert base.call_json(ctx, r, CostEstimate()) == {"results": []}
    assert base.call_json(ctx, r, CostEstimate()) == {"results": []}
    assert len(server.requests) == 1


# ---------- mock mode ----------

def test_mock_mode_uses_fixture_and_no_network(conn, tmp_path):
    fixtures = tmp_path / "fixtures" / "paidco"
    fixtures.mkdir(parents=True)
    (fixtures / "search.json").write_text(json.dumps({"mock": "default"}), encoding="utf-8")
    (fixtures / "search__acme.example.json").write_text(json.dumps({"mock": "acme"}),
                                                        encoding="utf-8")
    server = FakeServer(httpx.Response(500))
    ctx, _ = make_ctx(conn, server, mock=True, fixtures_dir=tmp_path / "fixtures")

    assert base.call_json(ctx, req(), CostEstimate(usd=9.99)) == {"mock": "default"}
    assert base.call_json(ctx, req(fixture_key="acme.example"), CostEstimate()) == {"mock": "acme"}
    assert server.requests == []
    assert db.month_spend(conn, now=NOW) == 0
    with pytest.raises(MockFixtureMissing):
        base.call_json(ctx, req(endpoint="nothing"), CostEstimate())
