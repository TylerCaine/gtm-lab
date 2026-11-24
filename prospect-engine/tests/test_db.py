"""Tests for src/db.py. Each test uses a fresh temporary database. Fake data only."""

from datetime import datetime, timedelta, timezone

import pytest

from src import db
from src.models import (
    AccountScore,
    Assessment,
    Company,
    Contact,
    FieldValue,
    Signal,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_schema_is_idempotent(conn):
    db.init_schema(conn)  # second time must not fail
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "companies", "contacts", "field_values", "signals", "assessments",
            "scores", "api_calls", "cache", "suppression"} <= tables


def test_company_upsert_no_duplicates_and_none_does_not_erase(conn):
    db.upsert_company(conn, Company(domain="acme.example", name="Acme", employee_count=120))
    db.upsert_company(conn, Company(domain="https://www.acme.example", industry="software"))
    assert _count(conn, "companies") == 1
    c = db.get_company(conn, "acme.example")
    assert c.name == "Acme" and c.employee_count == 120 and c.industry == "software"


def test_contact_upsert_and_duplicate_email_is_skipped(conn):
    cid = Contact.make_id("acme.example", "Jane Doe")
    jane = Contact(contact_id=cid, domain="acme.example", full_name="Jane Doe",
                   title="VP Sales", email="jane@acme.example", email_status="valid")
    assert db.upsert_contact(conn, jane)
    assert db.upsert_contact(conn, jane)  # same contact again: update, not duplicate
    assert _count(conn, "contacts") == 1

    other = Contact(contact_id=Contact.make_id("acme.example", "John Roe"),
                    domain="acme.example", full_name="John Roe", email="jane@acme.example")
    assert db.upsert_contact(conn, other) is False
    assert _count(conn, "contacts") == 1


def test_signals_dedupe(conn):
    s = Signal(domain="acme.example", signal_type="hiring", detail="SDR opening",
               source="theirstack", source_url="https://jobs.example/1", confidence=0.9)
    db.add_signal(conn, s)
    db.add_signal(conn, s.model_copy(update={"confidence": 0.8}))
    assert _count(conn, "signals") == 1
    assert db.get_signals(conn, "acme.example")[0].confidence == 0.8


def test_field_values_ignore_exact_repeats(conn):
    fv = FieldValue(entity_type="company", entity_id="acme.example", field="employee_count",
                    value=120, source="prospeo", confidence=0.9, retrieved_at=NOW)
    db.add_field_value(conn, fv)
    db.add_field_value(conn, fv)
    db.add_field_value(conn, fv.model_copy(update={"value": 130}))
    assert _count(conn, "field_values") == 2


def test_assessment_and_score_upsert(conn):
    a = Assessment(domain="acme.example", business_model="b2b_saas", sells_to="sales teams",
                   icp_fit="strong", fit_reasons=["fits"], disqualifiers=[],
                   evidence_urls=["https://acme.example/"], funding_evidence=None,
                   confidence=0.8, model="test-model")
    db.upsert_assessment(conn, a)
    db.upsert_assessment(conn, a.model_copy(update={"icp_fit": "medium"}))
    assert _count(conn, "assessments") == 1

    s = AccountScore(domain="acme.example", fit_score=80, intent_score=60, total=72,
                     tier="B", parts={"fit": {}})
    db.upsert_score(conn, s)
    db.upsert_score(conn, s.model_copy(update={"tier": "A"}))
    assert conn.execute("SELECT tier FROM scores").fetchone()[0] == "A"


def test_cache_hit_and_expiry(conn):
    db.cache_put(conn, "prospeo", "search-company", "h1", {"name": "Acme"}, now=NOW)
    assert db.cache_get(conn, "prospeo", "search-company", "h1", 90, now=NOW) == {"name": "Acme"}
    later = NOW + timedelta(days=8)
    assert db.cache_get(conn, "prospeo", "search-company", "h1", 90, now=later) is not None
    assert db.cache_get(conn, "prospeo", "search-company", "h1", 7, now=later) is None
    assert db.cache_get(conn, "prospeo", "search-company", "other", 90, now=NOW) is None


def test_spend_and_credit_totals(conn):
    run = "run-1"
    db.log_api_call(conn, run_id=run, provider="claude", endpoint="messages",
                    params_hash="a", status_code=200, est_cost_usd=0.02, now=NOW)
    db.log_api_call(conn, run_id=run, provider="claude", endpoint="messages",
                    params_hash="a", status_code=200, est_cost_usd=0.02, cached=True, now=NOW)
    db.log_api_call(conn, run_id=run, provider="theirstack", endpoint="jobs",
                    params_hash="b", status_code=200, credits=10, credit_bucket="api", now=NOW)
    # last month: counts toward nothing this month
    db.log_api_call(conn, run_id="old", provider="claude", endpoint="messages",
                    params_hash="c", status_code=200, est_cost_usd=5.0,
                    now=NOW - timedelta(days=30))

    assert db.run_spend(conn, run) == pytest.approx(0.02)
    assert db.month_spend(conn, now=NOW) == pytest.approx(0.02)
    assert db.month_credits(conn, "theirstack", "api", now=NOW) == 10
    assert db.month_credits(conn, "theirstack", "company", now=NOW) == 0


def test_runs_lifecycle(conn):
    run_id = db.new_run_id(NOW)
    assert run_id.startswith("20261124-120000-")
    db.create_run(conn, run_id, "icp_default", 5.0, now=NOW)
    db.log_api_call(conn, run_id=run_id, provider="claude", endpoint="messages",
                    params_hash="x", status_code=200, est_cost_usd=0.5, now=NOW)
    db.finish_run(conn, run_id, "done", now=NOW)
    row = conn.execute("SELECT status, spent_usd FROM runs WHERE run_id = ?",
                       (run_id,)).fetchone()
    assert row["status"] == "done" and row["spent_usd"] == pytest.approx(0.5)


def test_suppression_email_and_domain(conn, tmp_path):
    csv_path = tmp_path / "suppression.csv"
    db.add_suppression(conn, "Opted.Out@Acme.example", "opted out", csv_path=csv_path)
    db.add_suppression(conn, "https://www.blocked.example", "competitor", csv_path=csv_path)
    db.add_suppression(conn, "opted.out@acme.example", "dup", csv_path=csv_path)  # harmless

    assert db.is_suppressed(conn, email="opted.out@acme.example")
    assert not db.is_suppressed(conn, email="someone.else@acme.example")
    assert db.is_suppressed(conn, email="anyone@blocked.example")
    assert db.is_suppressed(conn, domain="blocked.example")
    assert len(csv_path.read_text(encoding="utf-8").strip().splitlines()) == 3  # header + 2

    fresh = db.connect(tmp_path / "fresh.db")
    assert db.load_suppression_csv(fresh, csv_path) == 2
    assert db.is_suppressed(fresh, domain="blocked.example")
    fresh.close()
