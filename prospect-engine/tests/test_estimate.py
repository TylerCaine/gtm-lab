"""Tests for `estimate` and the live-run confirmation gate. No network, no keys."""

import copy
from datetime import datetime, timezone

import pytest

from src import db, run
from src.config import load_icp, load_settings
from src.estimate import KEY_VARS, estimate_run, key_status

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
ICP = load_icp()
SETTINGS = load_settings()
REQUIRED = [name for name, req, _ in KEY_VARS if req]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_schema(c)
    yield c
    c.close()


def line(est, provider, endpoint_start):
    return next(l for l in est.lines if l.provider == provider
                and l.endpoint.startswith(endpoint_start))


def test_estimate_five_accounts(conn):
    est = estimate_run(conn, ICP, SETTINGS, 5, 2.0, now=NOW)
    assert est.candidates == 10
    assert line(est, "theirstack", "jobs").credits == 100          # 4 pages x 25 jobs
    assert line(est, "prospeo", "enrich").calls == 15              # 5 accounts x 3 contacts
    assert line(est, "hunter", "email-verifier").credits == 7.5    # 0.5 each
    q = line(est, "claude", "qualify")
    assert q.calls == 10 and 0 < q.usd_typical < q.usd_worst
    # Free plans log $0, so only Claude costs dollars
    assert est.usd_worst == pytest.approx(sum(l.usd_worst for l in est.lines
                                              if l.provider == "claude"))
    assert est.warnings == []


def test_estimate_warns_about_credit_caps_and_budget(conn):
    est = estimate_run(conn, ICP, SETTINGS, 25, 1.0, now=NOW)
    text = " ".join(est.warnings)
    assert "run budget" in text
    assert "prospeo" in text and "hunter" in text


def test_estimate_counts_credits_already_used_this_month(conn):
    db.log_api_call(conn, run_id="old", provider="prospeo", endpoint="enrich-person",
                    params_hash="x", status_code=200, credits=70, now=NOW)
    est = estimate_run(conn, ICP, SETTINGS, 5, 2.0, now=NOW)
    assert any("prospeo" in w and "70 used" in w for w in est.warnings)


def test_key_status_never_returns_values(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-secret-value")
    rows = key_status()
    assert ("TAVILY_API_KEY", True, True, rows[4][3]) in rows
    assert "tvly-secret-value" not in repr(rows)


def test_estimate_command_prints_without_spending(monkeypatch, capsys, tmp_path):
    settings = copy.deepcopy(SETTINGS)
    settings["paths"]["db"] = str(tmp_path / "real.db")
    monkeypatch.setattr(run, "load_settings", lambda *_: settings)
    for name, _, _ in KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    assert run.main(["estimate", "--accounts", "5", "--budget", "2"]) == 0
    out = capsys.readouterr().out
    assert "Estimate for 5 accounts" in out and "MISSING" in out
    c = db.connect(tmp_path / "real.db")
    assert c.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0
    c.close()


def _live_setup(monkeypatch, tmp_path, keys: bool, answer: str):
    settings = copy.deepcopy(SETTINGS)
    settings["paths"]["db"] = str(tmp_path / "real.db")
    settings["paths"]["output_dir"] = str(tmp_path / "out")
    settings["paths"]["logs_dir"] = str(tmp_path / "logs")
    monkeypatch.setattr(run, "load_settings", lambda *_: settings)
    monkeypatch.setattr(run, "load_dotenv", lambda: None)
    for name in REQUIRED:
        if keys:
            monkeypatch.setenv(name, "fake")
        else:
            monkeypatch.delenv(name, raising=False)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or answer)
    return asked


def _api_calls(tmp_path) -> int:
    c = db.connect(tmp_path / "real.db")
    n = c.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
    c.close()
    return n


def test_live_pipeline_stops_when_keys_missing(monkeypatch, tmp_path, capsys):
    asked = _live_setup(monkeypatch, tmp_path, keys=False, answer="yes")
    assert run.main(["pipeline", "--accounts", "5", "--budget", "2"]) == 1
    assert asked == [] and _api_calls(tmp_path) == 0
    assert "Nothing was called or spent" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["no", "", "y", "YES please"])
def test_live_pipeline_needs_exact_yes(monkeypatch, tmp_path, answer):
    asked = _live_setup(monkeypatch, tmp_path, keys=True, answer=answer)
    assert run.main(["pipeline", "--accounts", "5", "--budget", "2"]) == 1
    assert len(asked) == 1 and _api_calls(tmp_path) == 0


def test_dry_run_also_asks_first(monkeypatch, tmp_path):
    asked = _live_setup(monkeypatch, tmp_path, keys=True, answer="no")
    assert run.main(["pipeline", "--dry-run", "--accounts", "5"]) == 1
    assert "discovery only" in asked[0] and _api_calls(tmp_path) == 0


def test_mock_pipeline_never_asks(monkeypatch, tmp_path):
    asked = _live_setup(monkeypatch, tmp_path, keys=False, answer="no")
    settings = run.load_settings()
    settings["paths"]["mock_db"] = str(tmp_path / "mock.db")
    assert run.main(["pipeline", "--mock", "--accounts", "5"]) == 0
    assert asked == []
