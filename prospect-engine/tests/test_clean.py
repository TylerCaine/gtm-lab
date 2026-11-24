"""Tests for src/clean.py. Fake data only (*.example domains)."""

from src.clean import (
    clean_rows,
    company_key,
    domain_root,
    is_valid_email,
    normalize_name,
    normalize_phone,
)


def row(**overrides):
    base = {"company": "Acme", "domain": "acme.example", "contact": "Jane Doe",
            "title": "VP Sales", "email": "jane@acme.example", "email_status": "valid",
            "score": 80}
    base.update(overrides)
    return base


def test_normalizers():
    assert normalize_name("jane o'neil") == "Jane O'Neil"
    assert normalize_name("JANE DOE") == "Jane Doe"
    assert normalize_name("DeAndre McDonald") == "DeAndre McDonald"
    assert normalize_phone("(415) 555-0100") == "+14155550100"
    assert normalize_phone("not a phone") is None
    assert is_valid_email("a@b.example") and not is_valid_email("a@b") \
        and not is_valid_email("a b@c.example")
    assert company_key("Acme Software, Inc.") == "acme software"
    assert domain_root("app.acme.co.uk") == "acme"
    assert domain_root("acme.example") == "acme"


def test_normalization_is_recorded_and_input_untouched():
    original = row(domain="https://www.ACME.example/", contact="  jane   doe ",
                   email="Jane@Acme.Example")
    result = clean_rows([original], suppressed=set())
    out = result.rows[0]
    assert out["domain"] == "acme.example"
    assert out["contact"] == "Jane Doe"
    assert out["email"] == "jane@acme.example"
    rules = {c.rule for c in result.changes}
    assert {"trim", "normalize_domain", "name_case", "lowercase_email"} <= rules
    assert original["contact"] == "  jane   doe "


def test_suppression_by_email_and_domain():
    rows = [row(), row(contact="John Roe", email="john@blocked.example", domain="blocked.example"),
            row(contact="Sam Poe", email="sam@acme.example")]
    result = clean_rows(rows, suppressed={"jane@acme.example", "blocked.example"})
    assert [r["contact"] for r in result.rows] == ["Sam Poe"]
    assert sum(c.rule == "suppressed" for c in result.changes) == 2


def test_missing_required_field_goes_to_review():
    result = clean_rows([row(title="  ")], suppressed=set())
    assert result.rows == []
    assert "title" in result.needs_review[0]["review_reason"]


def test_invalid_email_syntax_cleared_and_invalid_status_dropped():
    result = clean_rows([row(email="not-an-email"),
                         row(contact="Sam Poe", email="sam@acme.example", email_status="invalid")],
                        suppressed=set())
    assert len(result.rows) == 1
    assert result.rows[0]["email"] is None and result.rows[0]["email_status"] == "no_email"
    assert any(c.rule == "email_status_invalid" for c in result.changes)


def test_duplicate_email_keeps_best_status():
    rows = [row(email_status="unknown", score=90), row(email_status="valid", score=70)]
    result = clean_rows(rows, suppressed=set())
    assert len(result.rows) == 1 and result.rows[0]["email_status"] == "valid"
    assert any(c.rule.startswith("duplicate_email") for c in result.changes)


def test_rows_without_email_are_kept():
    rows = [row(email=None, email_status="no_email"),
            row(contact="Sam Poe", email=None, email_status="no_email")]
    assert len(clean_rows(rows, suppressed=set()).rows) == 2


def test_similar_companies_flagged_not_merged():
    rows = [row(),
            row(company="Acme Inc.", domain="acme-hq.example", contact="Sam Poe",
                email="sam@acme-hq.example"),
            row(company="Globex", domain="globex.example", contact="Ann Lee",
                email="ann@globex.example")]
    result = clean_rows(rows, suppressed=set())
    assert len(result.rows) == 3                       # nothing merged
    flags = [r for r in result.needs_review if "possible duplicate" in r["review_reason"]]
    assert len(flags) == 1
    assert {flags[0]["domain"], flags[0]["similar_domain"]} == {"acme.example", "acme-hq.example"}
