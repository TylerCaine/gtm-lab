"""Tests for src/score.py using the real config files and fake companies."""

from datetime import date, datetime, timedelta, timezone

import pytest

from src.config import load_icp, load_scoring, load_settings
from src.models import Assessment, Company, FundingEvidence, Signal
from src.score import (
    ScoreInput,
    decay,
    inputs_from_records,
    score_account,
    tier_at_least,
)

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
ICP = load_icp()
SCORING = load_scoring()
TIERS = load_settings()["tiers"]


def perfect(**overrides) -> ScoreInput:
    data = dict(domain="acme.example", employee_count=120, business_model="b2b_saas",
                icp_fit="strong", tech_matched=True, hiring_dates=[TODAY] * 3,
                funding_date=TODAY, funding_confidence=1.0)
    data.update(overrides)
    return ScoreInput(**data)


def score(inp: ScoreInput):
    return score_account(inp, ICP, SCORING, TIERS, now=NOW)


def test_decay_halves_every_half_life():
    assert decay(0, 30) == 1.0
    assert decay(30, 30) == pytest.approx(0.5)
    assert decay(60, 30) == pytest.approx(0.25)
    assert decay(-5, 30) == 1.0


def test_perfect_account_is_tier_a_and_explained():
    s = score(perfect())
    assert (s.fit_score, s.intent_score, s.total, s.tier) == (100, 100, 100, "A")
    assert {"fit", "intent", "total"} <= set(s.parts)
    assert "employee_range" in s.parts["fit"]


def test_unknown_tech_does_not_lower_fit():
    assert score(perfect(tech_matched=None)).fit_score == 100
    assert "unknown" in score(perfect(tech_matched=None)).parts["fit"]["tech_match"]


def test_weak_fit_lowers_score():
    s = score(perfect(icp_fit="weak"))
    # (20 + 20 + 0.2*40 + 20) / 100 = 68
    assert s.fit_score == 68


def test_icp_fit_no_is_excluded_with_reason():
    s = score(perfect(icp_fit="no"))
    assert s.tier == "excluded" and "no" in s.exclusion_reason


def test_hiring_decay_and_missing_funding():
    s = score(perfect(hiring_dates=[TODAY - timedelta(days=30)], funding_date=None))
    # one job = 0.6, decayed 0.5, x 70 pts = 21 of 100 intent pts
    assert s.intent_score == 21


def test_low_total_is_excluded():
    s = score(perfect(icp_fit="weak", employee_count=5000, tech_matched=None,
                      hiring_dates=[], funding_date=None))
    assert s.tier == "excluded" and "below tier C" in s.exclusion_reason


def test_tier_at_least():
    assert tier_at_least("A", "B") and tier_at_least("B", "B")
    assert not tier_at_least("C", "B") and not tier_at_least("excluded", "C")


def test_inputs_from_records():
    signals = [
        Signal(domain="acme.example", signal_type="hiring", detail="SDR",
               source="theirstack", observed_at=NOW - timedelta(days=3), confidence=0.9),
        Signal(domain="acme.example", signal_type="tech", detail="HubSpot",
               source="theirstack", confidence=0.6),
        Signal(domain="acme.example", signal_type="funding", detail="Series A",
               source="tavily", confidence=0.4),
    ]
    assessment = Assessment(
        domain="acme.example", business_model="b2b_saas", sells_to="sales teams",
        icp_fit="strong", fit_reasons=[], disqualifiers=[], evidence_urls=[],
        funding_evidence=FundingEvidence(date=date(2026, 10, 1), round="Series A",
                                         url="https://news.example/a"),
        confidence=0.8, model="test")
    inp = inputs_from_records("acme.example", ICP, Company(domain="acme.example",
                              employee_count=120), assessment, signals)
    assert inp.tech_matched is True
    assert inp.hiring_dates == [TODAY - timedelta(days=3)]
    assert inp.funding_date == date(2026, 10, 1) and inp.funding_confidence == 0.4
