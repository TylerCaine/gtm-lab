"""Tests for TheirStack, Hunter, Tavily and Claude modules. No live calls: fixtures,
an httpx MockTransport, or a fake Anthropic client."""

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from src import db
from src.config import load_icp, load_settings
from src.models import QualificationResult, SignalType
from src.providers import claude, hunter, tavily, theirstack
from src.providers.base import CallContext, ProviderError, ProviderExhausted, ProviderHTTPError

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
ICP = load_icp()
SETTINGS = load_settings()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def mock_ctx(conn) -> CallContext:
    return CallContext(conn=conn, settings=SETTINGS, run_id="r1", mock=True,
                       sleep=lambda s: None, now=lambda: NOW)


def live_ctx(conn, handler, monkeypatch, env: str):
    monkeypatch.setenv(env, "fake-key-for-tests")
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    ctx = CallContext(conn=conn, settings=SETTINGS, run_id="r1",
                      http=httpx.Client(transport=httpx.MockTransport(wrapped)),
                      sleep=lambda s: None, now=lambda: NOW, clock=lambda: 0.0)
    return ctx, seen


def calls(conn):
    return conn.execute("SELECT * FROM api_calls ORDER BY id").fetchall()


# ---------- TheirStack ----------

def test_theirstack_body_uses_verified_fields():
    body = theirstack.build_job_search(ICP, SETTINGS, page=0)
    assert body["job_country_code_or"] == ["US"]
    assert body["posted_at_max_age_days"] == 30
    assert body["limit"] == 25 and body["page"] == 0
    assert "SDR" in body["job_title_or"]
    assert all("," not in t for t in body["job_title_or"])      # comma -> 422 per docs
    assert body["min_employee_count"] == 40 and body["max_employee_count"] == 600


def test_theirstack_groups_jobs_and_builds_signals(conn):
    data = theirstack.search_jobs(mock_ctx(conn), ICP, SETTINGS, page=0)
    companies = theirstack.group_by_company([data])
    acme = companies["acme-analytics.example"]
    assert len(acme.jobs) == 2 and "hubspot" in acme.technology_slugs
    assert acme.last_funding_round_date == date(2026, 10, 15)
    assert companies["staffpro.example"].is_recruiting_agency is True

    hiring = theirstack.hiring_signals(acme, 0.9)
    assert [s.signal_type for s in hiring] == [SignalType.HIRING] * 2
    assert hiring[0].source_url.startswith("https://careers.acme-analytics.example/")
    tech = theirstack.tech_signals(acme, ["HubSpot"], SETTINGS, 0.6)
    assert len(tech) == 1 and tech[0].confidence == 0.6
    # No HubSpot slug = no signal (unknown, never "no")
    assert theirstack.tech_signals(companies["brightpipe.example"], ["HubSpot"], SETTINGS, 0.6) == []


def test_theirstack_live_charges_per_job_and_sends_bearer(conn, monkeypatch):
    body = {"data": [{"id": 1, "company_domain": "a.example"}] * 3, "metadata": {}}
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(200, json=body), monkeypatch,
                         "THEIRSTACK_API_KEY")
    theirstack.search_jobs(ctx, ICP, SETTINGS, page=0)
    assert seen[0].headers["Authorization"] == "Bearer fake-key-for-tests"
    assert "fake-key" not in str(seen[0].url)
    assert calls(conn)[-1]["credits"] == 3 and calls(conn)[-1]["credit_bucket"] == "api"


def test_theirstack_402_marks_exhausted(conn, monkeypatch):
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(402, json={"error": {"code": "x"}}),
                      monkeypatch, "THEIRSTACK_API_KEY")
    with pytest.raises(ProviderExhausted):
        theirstack.search_jobs(ctx, ICP, SETTINGS, page=0)
    assert "theirstack" in ctx.exhausted


def test_theirstack_truncated_results_stop_further_calls(conn, monkeypatch):
    body = {"data": [], "metadata": {"truncated_results": 5}}
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(200, json=body), monkeypatch,
                      "THEIRSTACK_API_KEY")
    theirstack.search_jobs(ctx, ICP, SETTINGS, page=0)
    assert "theirstack" in ctx.exhausted


# ---------- Hunter ----------

def test_hunter_status_mapping():
    assert hunter.STATUS_MAP["accept_all"] == "catch_all"
    assert hunter.STATUS_MAP["webmail"] == "unknown"


def test_hunter_mock_finder_and_verifier(conn):
    ctx = mock_ctx(conn)
    found = hunter.find_email(ctx, "acme-analytics.example", "Sam", "Poe")
    assert found.email == "sam.poe@acme-analytics.example" and found.status is None
    assert hunter.verify_email(ctx, found.email) == "catch_all"
    assert hunter.find_email(ctx, "nobody.example", "No", "One").email is None


def test_hunter_uses_header_not_url(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(200, json={"data": {"email": None}}),
                         monkeypatch, "HUNTER_API_KEY")
    hunter.find_email(ctx, "a.example", "Jo", "Fake")
    assert seen[0].headers["X-API-KEY"] == "fake-key-for-tests"
    assert "fake-key" not in str(seen[0].url) and "api_key" not in str(seen[0].url)
    assert calls(conn)[-1]["credits"] == 0          # not found -> not charged


def test_hunter_404_is_not_found_and_cached(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(404, json={"errors": []}),
                         monkeypatch, "HUNTER_API_KEY")
    assert hunter.find_email(ctx, "a.example", "Jo", "Fake").email is None
    assert hunter.find_email(ctx, "a.example", "Jo", "Fake").email is None
    assert len(seen) == 1                           # second answer came from cache


def test_hunter_429_means_out_of_credits_not_retry(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(
        429, json={"errors": [{"id": "too_many_requests", "details": "usage limit"}]}),
        monkeypatch, "HUNTER_API_KEY")
    with pytest.raises(ProviderExhausted):
        hunter.find_email(ctx, "a.example", "Jo", "Fake")
    assert len(seen) == 1 and "hunter" in ctx.exhausted


def test_hunter_403_rate_limit_retries(conn, monkeypatch):
    answers = iter([httpx.Response(403, json={"errors": [{"details": "Rate limit reached"}]}),
                    httpx.Response(200, json={"data": {"status": "valid"}})])
    ctx, seen = live_ctx(conn, lambda r: next(answers), monkeypatch, "HUNTER_API_KEY")
    assert hunter.verify_email(ctx, "jo@a.example") == "valid"
    assert len(seen) == 2


def test_hunter_222_records_unknown(conn, monkeypatch):
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(222, json={}), monkeypatch, "HUNTER_API_KEY")
    assert hunter.verify_email(ctx, "jo@a.example") == "unknown"


# ---------- Tavily ----------

def test_tavily_cost_estimates():
    assert tavily.estimate_cost("search").credits == 1
    assert tavily.estimate_cost("extract", 3).credits == 1
    assert tavily.estimate_cost("extract", 6).credits == 2


def test_tavily_mock_extract_and_search(conn):
    ctx = mock_ctx(conn)
    out = tavily.extract(ctx, ["https://acme-analytics.example/"], fixture_key="acme-analytics.example")
    assert "https://acme-analytics.example/pricing" in out["pages"]
    res = tavily.funding_search(ctx, "DataLoom", 180, NOW.date(), fixture_key="dataloom.example")
    assert res[0]["url"] == "https://news.example/dataloom-series-b"


def test_tavily_search_body_and_usage_credits(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(
        200, json={"results": [], "usage": {"credits": 1}}), monkeypatch, "TAVILY_API_KEY")
    tavily.funding_search(ctx, "Acme", 180, date(2026, 11, 24))
    sent = json.loads(seen[0].content)
    assert sent["start_date"] == "2026-05-28" and sent["topic"] == "news"
    assert sent["include_usage"] is True and sent["search_depth"] == "basic"
    assert seen[0].headers["Authorization"] == "Bearer fake-key-for-tests"
    assert calls(conn)[-1]["credits"] == 1 and calls(conn)[-1]["est_cost_usd"] == 0  # free plan


def test_tavily_432_is_exhausted(conn, monkeypatch):
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(432, json={"detail": "limit"}),
                      monkeypatch, "TAVILY_API_KEY")
    with pytest.raises(ProviderExhausted):
        tavily.extract(ctx, ["https://a.example/"])


# ---------- Claude ----------

GOOD = QualificationResult(business_model="b2b_saas", sells_to="teams", icp_fit="strong",
                           fit_reasons=["r"], disqualifiers=[], evidence_urls=[],
                           funding_evidence=None, confidence=0.8)


def fake_response(parsed=GOOD, stop="end_turn", model="claude-sonnet-5-5"):
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed, model=model,
                           usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000,
                                                 cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = SimpleNamespace(parse=lambda **kw: self._answer("plain", kw))
        self.beta = SimpleNamespace(messages=SimpleNamespace(
            parse=lambda **kw: self._answer("beta", kw)))

    def _answer(self, kind, kwargs):
        self.calls.append((kind, kwargs))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def claude_ctx(conn, monkeypatch, answers):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    fake = FakeClient(answers)
    monkeypatch.setattr(claude, "_client", lambda: fake)
    ctx = CallContext(conn=conn, settings=SETTINGS, run_id="r1", sleep=lambda s: None,
                      now=lambda: NOW, clock=lambda: 0.0)
    return ctx, fake


def ask(ctx, model="claude-sonnet-5-5"):
    return claude.structured(ctx, task="qualify", model=model, system="s", user="u",
                             schema=QualificationResult)


def test_claude_sonnet_uses_fallback_and_logs_token_cost(conn, monkeypatch):
    ctx, fake = claude_ctx(conn, monkeypatch, [fake_response()])
    assert ask(ctx).icp_fit == "strong"
    kind, kw = fake.calls[0]
    assert kind == "beta" and kw["fallbacks"] == "default"
    assert kw["betas"] == [claude.FALLBACK_BETA] and kw["output_format"] is QualificationResult
    # 1M input x $2 + 100K output x $10/M = $3.00
    assert calls(conn)[-1]["est_cost_usd"] == pytest.approx(3.0)


def test_claude_haiku_uses_plain_parse(conn, monkeypatch):
    ctx, fake = claude_ctx(conn, monkeypatch, [fake_response(model="claude-haiku-5-5")])
    ask(ctx, model="claude-haiku-5-5")
    assert fake.calls[0][0] == "plain"


def test_claude_answers_are_cached(conn, monkeypatch):
    ctx, fake = claude_ctx(conn, monkeypatch, [fake_response()])
    ask(ctx)
    ask(ctx)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_claude_refusal_and_truncation_fail_cleanly(conn, monkeypatch, stop):
    ctx, _ = claude_ctx(conn, monkeypatch, [fake_response(parsed=None, stop=stop)])
    with pytest.raises(ProviderError):
        ask(ctx)


def test_claude_retries_connection_errors(conn, monkeypatch):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    ctx, fake = claude_ctx(conn, monkeypatch, [err, fake_response()])
    assert ask(ctx).icp_fit == "strong"
    assert len(fake.calls) == 2


def test_claude_auth_error_is_not_retried(conn, monkeypatch):
    req = httpx.Request("POST", "https://api.anthropic.com")
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=req),
                                        body=None)
    ctx, fake = claude_ctx(conn, monkeypatch, [err])
    with pytest.raises(ProviderHTTPError):
        ask(ctx)
    assert len(fake.calls) == 1


def test_claude_budget_guard_blocks_expensive_call(conn, monkeypatch):
    ctx, fake = claude_ctx(conn, monkeypatch, [fake_response()])
    ctx.budget_usd = 0.0001
    with pytest.raises(ProviderError):
        ask(ctx)
    assert fake.calls == []


def test_claude_records_model_that_actually_answered(conn, monkeypatch):
    resp = fake_response(model="claude-opus-5-5")
    resp.usage.iterations = [SimpleNamespace(type="message"),
                             SimpleNamespace(type="fallback_message")]
    ctx, _ = claude_ctx(conn, monkeypatch, [resp])
    answer = claude.structured_answer(ctx, task="qualify", model="claude-sonnet-5-5",
                                      system="s", user="u", schema=QualificationResult)
    assert answer.model == "claude-opus-5-5" and answer.fallback_used is True
    # Cached answers keep the served model too
    again = claude.structured_answer(ctx, task="qualify", model="claude-sonnet-5-5",
                                     system="s", user="u", schema=QualificationResult)
    assert again.model == "claude-opus-5-5"


def test_qualify_stores_served_model_in_assessments(conn, tmp_path):
    import shutil
    from src.qualify import qualify
    from src.config import load_scoring
    fx = tmp_path / "fixtures"
    shutil.copytree("tests/fixtures", fx)
    path = fx / "claude" / "qualify__acme-analytics.example.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(model="claude-opus-5-5", fallback_used=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    db.init_schema(conn)
    ctx = CallContext(conn=conn, settings=SETTINGS, run_id="r1", mock=True, fixtures_dir=fx,
                      sleep=lambda s: None, now=lambda: NOW)
    qualify(ctx, ICP, SETTINGS, load_scoring(), ["acme-analytics.example", "brightpipe.example"], {})
    assert db.get_assessment(conn, "acme-analytics.example").model == "claude-opus-5-5"
    assert db.get_assessment(conn, "brightpipe.example").model == "claude-sonnet-5-5"
