"""Tests for the CLI in src/run.py (temporary database, fake data)."""

import argparse
import logging
from datetime import datetime, timezone

import pytest

from src import db, run
from src.config import load_settings


@pytest.fixture
def settings(tmp_path):
    s = load_settings()
    s["paths"] = {**s["paths"],
                  "db": str(tmp_path / "prospects.db"),
                  "mock_db": str(tmp_path / "mock.db"),
                  "logs_dir": str(tmp_path / "logs"),
                  "suppression_file": str(tmp_path / "suppression.csv")}
    return s


def test_parser_reads_pipeline_options():
    args = run.build_parser().parse_args(
        ["pipeline", "--accounts", "5", "--budget", "2", "--mock"])
    assert (args.command, args.accounts, args.budget, args.mock, args.dry_run) == \
        ("pipeline", 5, 2.0, True, False)


def test_mock_and_dry_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        run.build_parser().parse_args(["pipeline", "--mock", "--dry-run"])


def test_suppress_add_writes_db_and_private_csv(settings, tmp_path):
    args = argparse.Namespace(value="Opted.Out@Acme.example", reason="opted out")
    assert run.cmd_suppress(args, settings) == 0
    conn = db.connect(tmp_path / "prospects.db")
    assert db.is_suppressed(conn, email="opted.out@acme.example")
    conn.close()
    assert "opted.out@acme.example" in (tmp_path / "suppression.csv").read_text(encoding="utf-8")


def test_suppress_add_rejects_garbage(settings):
    assert run.cmd_suppress(argparse.Namespace(value="nonsense", reason=None), settings) == 1


def test_report_shows_costs(settings, tmp_path, capsys):
    conn = db.connect(tmp_path / "prospects.db")
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    db.create_run(conn, "r1", "icp_default", 5.0, now=now)
    db.log_api_call(conn, run_id="r1", provider="claude", endpoint="messages",
                    params_hash="a", status_code=200, est_cost_usd=0.0123, now=now)
    db.log_api_call(conn, run_id="r1", provider="claude", endpoint="messages",
                    params_hash="a", status_code=200, cached=True, now=now)
    db.log_api_call(conn, run_id="r1", provider="prospeo", endpoint="search-company",
                    params_hash="b", status_code=None, error="refused: run budget", now=now)
    conn.close()

    assert run.cmd_report(argparse.Namespace(run="r1", mock=False), settings) == 0
    out = capsys.readouterr().out
    assert "claude" in out and "prospeo" in out and "0.0123" in out


def test_report_unknown_run(settings):
    assert run.cmd_report(argparse.Namespace(run="nope", mock=False), settings) == 1


def test_setup_logging_writes_run_log(settings):
    path = run.setup_logging(settings, run_id="r-test")
    logging.getLogger("prospect_engine.test").debug("hello debug")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello debug" in path.read_text(encoding="utf-8")
    run.setup_logging(settings)  # detach the file handler again
