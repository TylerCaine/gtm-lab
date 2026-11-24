"""Tests for src/providers/prospeo.py: fixtures + fake network only, no live calls."""

import copy
from datetime import date, datetime, timezone

import httpx
import pytest

from src import db
from src.config import load_icp, load_settings
from src.providers import prospeo
from src.providers.base import (
    CallContext,
    ProviderExhausted,
    ProviderHTTPError,
)

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


def live_ctx(conn, handler, monkeypatch) -> tuple[CallContext, list[httpx.Request]]:
    """A 'live' context whose network is a fake handler."""
    monkeypatch.setenv("PROSPEO_API_KEY", "fake-key-for-tests")
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    ctx = CallContext(conn=conn, settings=SETTINGS, run_id="r1",
                      http=httpx.Client(transport=httpx.MockTransport(wrapped)),
                      sleep=lambda s: None, now=lambda: NOW)
    return ctx, seen


# ---------- filters ----------

def test_company_filters_default_free_plan():
    f = prospeo.build_company_filters(ICP, SETTINGS)
    assert f["company_location_search"] == {"include": ["United States"]}
    assert f["company_headcount_custom"] == {"min": 50, "max": 500}
    assert f["company_type"] == {"subtypes": {"include": ["SaaS"]}}
    assert "company_technology" not in f          # needs Starter plan; off by default
    assert "company_headcount_range" not in f     # can't combine with custom


def test_company_filters_with_technology_and_ranges():
    s = copy.deepcopy(SETTINGS)
    s["providers"]["prospeo"]["search"].update(use_technology_filter=True, headcount_mode="ranges")
    f = prospeo.build_company_filters(ICP, s)
    assert f["company_technology"] == {"include": ["HubSpot"]}
    assert f["company_headcount_range"] == ["21-50", "51-100", "101-200", "201-500"]
    assert "company_headcount_custom" not in f


def test_headcount_ranges():
    assert prospeo.headcount_ranges(51, 500) == ["51-100", "101-200", "201-500"]
    assert prospeo.headcount_ranges(5000, 20000)[-1] == "10000+"


def test_person_filters():
    f = prospeo.build_person_filters(["acme-analytics.example"], ICP, SETTINGS)
    assert f["company"] == {"websites": {"include": ["acme-analytics.example"]}}
    assert f["person_job_title"]["match_mode"] == "CONTAINS"
    assert "VP Sales" in f["person_job_title"]["include"]
    assert f["person_seniority"] == {"include": ["C-Suite", "Vice President", "Director", "Head"]}
    assert f["person_location_search"] == {"include": ["United States"]}
    assert f["max_person_per_company"] == 5


def test_search_people_rejects_too_many_domains(conn):
    with pytest.raises(ValueError):
        prospeo.search_people(mock_ctx(conn), [f"d{i}.example" for i in range(501)], ICP, SETTINGS)


# ---------- parsing (mock fixtures) ----------

def test_parse_companies_from_fixture(conn):
    data = prospeo.search_companies(mock_ctx(conn), ICP, SETTINGS)
    parsed = [prospeo.parse_company(r["company"]) for r in data["results"]]
    acme = parsed[0]
    assert acme.company.domain == "acme-analytics.example"
    assert acme.company.country == "US" and acme.company.employee_count == 120
    assert acme.technologies == ["HubSpot", "Slack"]
    assert acme.latest_funding_date == date(2026, 10, 15)
    assert acme.funding_link == "https://news.example/acme-analytics-series-a"
    assert acme.active_job_titles[0] == "Sales Development Representative"
    assert parsed[2].technologies is None            # CloudNest: no tech data = unknown


def test_tech_signals_match_only_never_no(conn):
    data = prospeo.search_companies(mock_ctx(conn), ICP, SETTINGS)
    parsed = [prospeo.parse_company(r["company"]) for r in data["results"]]
    acme, bright, cloud = parsed[0], parsed[1], parsed[2]
    sig = prospeo.tech_signals(acme, ["HubSpot"], 0.6)
    assert len(sig) == 1 and sig[0].source == "prospeo" and sig[0].confidence == 0.6
    assert prospeo.tech_signals(bright, ["HubSpot"], 0.6) == []   # other tech -> unknown
    assert prospeo.tech_signals(cloud, ["HubSpot"], 0.6) == []    # no data -> unknown


def test_parse_people_from_fixture(conn):
    data = prospeo.search_people(mock_ctx(conn), ["acme-analytics.example"], ICP, SETTINGS)
    people = [prospeo.parse_person(r) for r in data["results"]]
    jane = people[0]
    assert (jane.person_id, jane.full_name, jane.title) == ("p-fake-acme-1", "Jane Doe", "VP Sales")
    assert jane.seniority == "vp" and jane.domain == "acme-analytics.example"


def test_enrich_email_found_and_not_found(conn):
    ctx = mock_ctx(conn)
    found = prospeo.enrich_email(ctx, "p-fake-acme-1", SETTINGS)
    assert found.email == "jane.doe@acme-analytics.example" and found.status == "valid"
    missing = prospeo.enrich_email(ctx, "p-fake-acme-2", SETTINGS)   # default fixture: NO_MATCH
    assert missing.email is None and missing.status is None


def test_mock_mode_needs_no_api_key(conn, monkeypatch):
    monkeypatch.delenv("PROSPEO_API_KEY", raising=False)
    assert prospeo.account_information(mock_ctx(conn))["remaining_credits"] == 75


# ---------- live behavior with a fake network ----------

def test_request_shape_and_auth_header(conn, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"error": False, "free": False, "results": [{}],
                                         "pagination": {}})
    ctx, seen = live_ctx(conn, handler, monkeypatch)
    prospeo.search_companies(ctx, ICP, SETTINGS)
    req = seen[0]
    assert req.method == "POST" and str(req.url) == "https://api.prospeo.io/search-company"
    assert req.headers["x-key"] == "fake-key-for-tests"
    assert "fake-key-for-tests" not in str(req.url)
    assert db.month_credits(conn, "prospeo", now=NOW) == 1


def test_no_match_is_cached_and_free(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(
        400, json={"error": True, "error_code": "NO_MATCH"}), monkeypatch)
    assert prospeo.enrich_email(ctx, "p-x", SETTINGS).email is None
    assert prospeo.enrich_email(ctx, "p-x", SETTINGS).email is None
    assert len(seen) == 1                                   # second answer came from cache
    assert db.month_credits(conn, "prospeo", now=NOW) == 0


def test_free_repeat_page_costs_no_credit(conn, monkeypatch):
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(
        200, json={"error": False, "free": True, "results": [{}], "pagination": {}}), monkeypatch)
    prospeo.search_companies(ctx, ICP, SETTINGS)
    assert db.month_credits(conn, "prospeo", now=NOW) == 0


def test_insufficient_credits_blocks_further_calls(conn, monkeypatch):
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(
        400, json={"error": True, "error_code": "INSUFFICIENT_CREDITS"}), monkeypatch)
    with pytest.raises(ProviderExhausted):
        prospeo.enrich_email(ctx, "p-1", SETTINGS)
    with pytest.raises(ProviderExhausted):
        prospeo.enrich_email(ctx, "p-2", SETTINGS)
    assert len(seen) == 1                                   # second call never sent
    assert "prospeo" in ctx.exhausted


def test_plan_required_explains_fix(conn, monkeypatch):
    ctx, _ = live_ctx(conn, lambda r: httpx.Response(
        400, json={"error": True, "error_code": "PLAN_REQUIRED",
                   "filter_error": "company_technology requires STARTER+"}), monkeypatch)
    with pytest.raises(ProviderHTTPError, match="use_technology_filter"):
        prospeo.search_companies(ctx, ICP, SETTINGS)


def test_service_unavailable_is_retried(conn, monkeypatch):
    responses = [httpx.Response(400, json={"error": True,
                                           "error_code": "SERVICE_TEMPORARILY_UNAVAILABLE"}),
                 httpx.Response(200, json={"error": False, "results": [], "pagination": {}})]
    ctx, seen = live_ctx(conn, lambda r: responses.pop(0), monkeypatch)
    prospeo.search_companies(ctx, ICP, SETTINGS)
    assert len(seen) == 2


def test_account_information_is_never_cached(conn, monkeypatch):
    body = {"error": False, "response": {"remaining_credits": 70}}
    ctx, seen = live_ctx(conn, lambda r: httpx.Response(200, json=body), monkeypatch)
    prospeo.account_information(ctx)
    prospeo.account_information(ctx)
    assert len(seen) == 2 and seen[0].method == "GET"
