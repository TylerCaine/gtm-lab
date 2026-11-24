"""Tests for src/models.py. Fake data only (*.example domains)."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.models import (
    ICP,
    Company,
    Contact,
    QualificationResult,
    Signal,
    normalize_domain,
)

ICP_PATH = Path(__file__).resolve().parents[1] / "config" / "icp_default.json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.example", "acme.example"),
        ("https://www.Acme.example/about?x=1", "acme.example"),
        ("http://acme.example:8080", "acme.example"),
        ("jane.doe@Acme.example", "acme.example"),
        ("  WWW.acme.example.  ", "acme.example"),
    ],
)
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("bad", ["", "localhost", "not a domain.example"])
def test_normalize_domain_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_domain(bad)


def test_default_icp_file_is_valid() -> None:
    icp = ICP.model_validate(json.loads(ICP_PATH.read_text(encoding="utf-8")))
    assert icp.hard_filters.country == "US"
    assert icp.target_accounts == 25
    assert icp.personas[0].priority == 1


def _icp_dict() -> dict:
    return json.loads(ICP_PATH.read_text(encoding="utf-8"))


def test_icp_rejects_non_us_country() -> None:
    data = _icp_dict()
    data["hard_filters"]["country"] = "CA"
    with pytest.raises(ValidationError):
        ICP.model_validate(data)


def test_icp_rejects_inverted_employee_range() -> None:
    data = _icp_dict()
    data["hard_filters"]["employee_min"] = 600
    with pytest.raises(ValidationError):
        ICP.model_validate(data)


def test_icp_rejects_unknown_fields() -> None:
    data = _icp_dict()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        ICP.model_validate(data)


def test_icp_schema_forbids_additional_properties() -> None:
    schema = QualificationResult.model_json_schema()
    assert schema["additionalProperties"] is False


def test_qualification_result_parses_funding_date() -> None:
    result = QualificationResult.model_validate({
        "business_model": "b2b_saas",
        "sells_to": "Sales teams",
        "icp_fit": "strong",
        "fit_reasons": ["Sells CRM add-ons to B2B sales teams"],
        "disqualifiers": [],
        "evidence_urls": ["https://acme.example/"],
        "funding_evidence": {"date": "2026-09-01", "round": "Series A",
                             "url": "https://news.example/acme-series-a"},
        "confidence": 0.8,
    })
    assert result.funding_evidence is not None
    assert result.funding_evidence.date.year == 2026


def test_company_domain_is_normalized() -> None:
    assert Company(domain="https://www.Acme.example/").domain == "acme.example"


def test_contact_id_is_stable_and_email_lowercased() -> None:
    a = Contact.make_id("acme.example", "Jane  Doe")
    b = Contact.make_id("https://www.ACME.example", "jane doe")
    assert a == b
    c = Contact(contact_id=a, domain="acme.example", full_name="Jane Doe",
                email=" Jane.Doe@Acme.Example ")
    assert c.email == "jane.doe@acme.example"


def test_confidence_must_be_between_0_and_1() -> None:
    with pytest.raises(ValidationError):
        Signal(domain="acme.example", signal_type="tech", detail="HubSpot",
               source="theirstack", confidence=1.5)
