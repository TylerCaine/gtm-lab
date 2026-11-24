"""End-to-end mock pipeline: fixtures only, writes CSVs into a temp folder."""

import copy
import csv
from datetime import datetime, timezone

import pytest

from src import db
from src.config import load_icp, load_scoring, load_settings
from src.contacts import classify_by_rules
from src.export import PROSPECT_COLUMNS
from src.pipeline import run_pipeline
from src.providers.base import CallContext
from src.qualify import validate_evidence
from src.models import FundingEvidence, QualificationResult

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
ICP = load_icp()


@pytest.fixture
def result(tmp_path):
    settings = copy.deepcopy(load_settings())
    settings["paths"]["output_dir"] = str(tmp_path / "output")
    conn = db.connect(tmp_path / "mock.db")
    db.init_schema(conn)
    ctx = CallContext(conn=conn, settings=settings, run_id="test-run", mock=True,
                      sleep=lambda s: None, now=lambda: NOW)
    db.create_run(conn, "test-run", ICP.name, 5.0, mock=True, now=NOW)
    res = run_pipeline(ctx, ICP, settings, load_scoring(), target_accounts=25)
    yield res, conn
    conn.close()


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_mock_pipeline_end_to_end(result):
    res, _ = result
    assert res.candidates == 4
    assert res.tiers == {"A": 2, "B": 1, "excluded": 1}
    assert res.spent_usd == 0

    rows = read(res.paths["prospects"])
    assert list(rows[0].keys()) == PROSPECT_COLUMNS
    assert len(rows) == 5
    assert all(r["approved"] == "" for r in rows)                   # spec: always empty
    assert all(r["domain"].endswith(".example") for r in rows)
    assert [r["tier"] for r in rows] == sorted(r["tier"] for r in rows)   # A before B
    sam = next(r for r in rows if r["contact"] == "Sam Poe")
    assert sam["email_status"] == "catch_all" and "hunter" in sam["sources"]
    assert not any(r["domain"] == "cloudnest.example" for r in rows)  # excluded: B2C


def test_mock_pipeline_accounts_explain_exclusions(result):
    res, _ = result
    accounts = {r["domain"]: r for r in read(res.paths["accounts"])}
    assert accounts["northwind-dev.example"]["status"].startswith("650 employees")
    assert accounts["maple-saas.example"]["status"].startswith("not US")
    assert accounts["cloudnest.example"]["status"] == "Claude judged ICP fit = no"
    assert "staffpro.example" in accounts


def test_mock_pipeline_honours_suppression(tmp_path):
    settings = copy.deepcopy(load_settings())
    settings["paths"]["output_dir"] = str(tmp_path / "output")
    conn = db.connect(tmp_path / "mock.db")
    db.init_schema(conn)
    db.add_suppression(conn, "dataloom.example", "test")
    ctx = CallContext(conn=conn, settings=settings, run_id="r2", mock=True,
                      sleep=lambda s: None, now=lambda: NOW)
    db.create_run(conn, "r2", ICP.name, 5.0, mock=True, now=NOW)
    res = run_pipeline(ctx, ICP, settings, load_scoring(), target_accounts=25)
    rows = read(res.paths["prospects"])
    assert rows and not any(r["domain"] == "dataloom.example" for r in rows)
    conn.close()


def test_mock_run_is_idempotent(result, tmp_path):
    res, conn = result
    before = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    settings = copy.deepcopy(load_settings())
    settings["paths"]["output_dir"] = str(tmp_path / "output2")
    ctx = CallContext(conn=conn, settings=settings, run_id="test-run-2", mock=True,
                      sleep=lambda s: None, now=lambda: NOW)
    db.create_run(conn, "test-run-2", ICP.name, 5.0, mock=True, now=NOW)
    run_pipeline(ctx, ICP, settings, load_scoring(), target_accounts=25)
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == before


def test_title_rules():
    assert classify_by_rules("VP Sales", ICP).persona == "economic_buyer"
    assert classify_by_rules("Director of Sales", ICP).persona == "head_of_sales"
    assert classify_by_rules("Executive Assistant to the VP Sales", ICP).is_match is False
    assert classify_by_rules("Sales Development Representative", ICP).is_match is False
    assert classify_by_rules("Revenue Operations Lead", ICP) is None   # left to Claude


def test_validate_evidence_drops_invented_urls():
    r = QualificationResult(business_model="b2b_saas", sells_to="x", icp_fit="strong",
                            fit_reasons=[], disqualifiers=[],
                            evidence_urls=["https://a.example/", "https://made-up.example/"],
                            funding_evidence=FundingEvidence(date=None, round="A",
                                                             url="https://made-up.example/news"),
                            confidence=0.8)
    out = validate_evidence(r, {"https://a.example/"})
    assert out.evidence_urls == ["https://a.example/"]
    assert out.funding_evidence is None
    assert out.confidence == 0.6
