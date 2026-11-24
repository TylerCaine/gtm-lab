"""Pydantic data models shared by every pipeline stage.

A model describes what a piece of data must look like. When data from a
provider or from Claude does not fit, Pydantic raises a ValidationError
immediately, so bad data never reaches the database.

Models that Claude fills in (ICP, QualificationResult, TitleClassification)
forbid extra fields, which becomes ``additionalProperties: false`` in their
JSON schema, as Claude structured outputs require.
"""

import datetime as dt
import hashlib
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# A confidence value: 0 = no confidence, 1 = certain.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
# A score on a 0-100 scale.
Score100 = Annotated[float, Field(ge=0.0, le=100.0)]


def utc_now() -> datetime:
    """Return the current time in UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")


def normalize_domain(value: str) -> str:
    """Turn a URL, email or messy domain into a bare lowercase domain.

    Examples: "https://www.Acme.example/about" -> "acme.example",
    "jane@acme.example" -> "acme.example".

    Raises:
        ValueError: if the result does not look like a domain.
    """
    d = value.strip().lower()
    d = _SCHEME_RE.sub("", d)                  # drop "https://"
    d = re.split(r"[/?#]", d, maxsplit=1)[0]   # drop path, query, fragment
    d = d.rsplit("@", 1)[-1]                   # keep the part after "@"
    d = d.split(":", 1)[0]                     # drop ":443"
    d = d.rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if not d or "." not in d or " " in d:
        raise ValueError(f"not a valid domain: {value!r}")
    return d


class StrictModel(BaseModel):
    """Base class that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


# ---------- Enums (fixed lists of allowed values) ----------

class BusinessModel(StrEnum):
    B2B_SAAS = "b2b_saas"
    B2B_SERVICES = "b2b_services"
    B2C = "b2c"
    MARKETPLACE = "marketplace"
    HARDWARE = "hardware"
    OTHER = "other"
    UNKNOWN = "unknown"


class IcpFit(StrEnum):
    STRONG = "strong"
    MEDIUM = "medium"
    WEAK = "weak"
    NO = "no"


class Seniority(StrEnum):
    C_SUITE = "c_suite"
    VP = "vp"
    HEAD = "head"
    DIRECTOR = "director"
    MANAGER = "manager"
    INDIVIDUAL_CONTRIBUTOR = "individual_contributor"
    UNKNOWN = "unknown"


class EmailStatus(StrEnum):
    VALID = "valid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    NO_EMAIL = "no_email"


class PhoneType(StrEnum):
    MOBILE = "mobile"
    LANDLINE = "landline"
    UNKNOWN = "unknown"


class SignalType(StrEnum):
    HIRING = "hiring"
    TECH = "tech"
    FUNDING = "funding"


class Tier(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    EXCLUDED = "excluded"


# ---------- ICP (Ideal Customer Profile) ----------

class HardFilters(StrictModel):
    """Rules a company must pass. Failing any of these removes it."""

    country: Literal["US"] = "US"  # spec rule 1: US companies only
    employee_min: int = Field(ge=1)
    employee_max: int = Field(ge=1)
    business_model: BusinessModel

    @model_validator(mode="after")
    def _check_range(self) -> "HardFilters":
        if self.employee_min > self.employee_max:
            raise ValueError("employee_min must be <= employee_max")
        return self


class SoftSignals(StrictModel):
    """Signals that raise a company's score but don't remove it when missing."""

    hiring_titles: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    funding_within_days: int | None = Field(default=None, ge=1)


class Persona(StrictModel):
    """A type of person we want to reach. Lower priority number = preferred."""

    name: str
    priority: int = Field(default=1, ge=1)
    titles: list[str] = Field(min_length=1)
    seniorities: list[Seniority] = Field(default_factory=list)


class Exclusions(StrictModel):
    """Domains and industries that are never included."""

    domains: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)

    @field_validator("domains")
    @classmethod
    def _normalize_domains(cls, v: list[str]) -> list[str]:
        return [normalize_domain(d) for d in v]

    @field_validator("industries")
    @classmethod
    def _lowercase_industries(cls, v: list[str]) -> list[str]:
        return [i.strip().lower() for i in v]


class ICP(StrictModel):
    """The full Ideal Customer Profile that drives a pipeline run."""

    name: str
    description: str = ""
    hard_filters: HardFilters
    soft_signals: SoftSignals = Field(default_factory=SoftSignals)
    personas: list[Persona] = Field(min_length=1)
    exclusions: Exclusions = Field(default_factory=Exclusions)
    target_accounts: int = Field(ge=1, le=500)


# ---------- Records we store ----------

class Company(BaseModel):
    """One company, identified by its normalized domain."""

    domain: str
    name: str | None = None
    employee_count: int | None = Field(default=None, ge=0)
    country: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None  # only if a licensed API returned it

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)


class Contact(BaseModel):
    """One person at a company."""

    contact_id: str
    domain: str
    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    persona: str | None = None
    seniority: Seniority | None = None
    email: str | None = None
    email_status: EmailStatus | None = None  # None = not enriched yet
    phone: str | None = None
    phone_type: PhoneType | None = None

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, v: str | None) -> str | None:
        return v.strip().lower() if v else None

    @staticmethod
    def make_id(domain: str, full_name: str) -> str:
        """Build a stable ID from domain + name, so re-runs update instead of duplicating."""
        key = f"{normalize_domain(domain)}|{' '.join(full_name.lower().split())}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class Signal(BaseModel):
    """Evidence that a company is in-market (hiring, tech used, recent funding)."""

    domain: str
    signal_type: SignalType
    detail: str
    observed_at: datetime | None = None
    source: str                      # provider name, e.g. "theirstack"
    source_url: str | None = None
    confidence: Confidence

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)


class FieldValue(BaseModel):
    """One value received from one provider, kept for provenance."""

    entity_type: Literal["company", "contact"]
    entity_id: str
    field: str
    value: Any
    source: str
    retrieved_at: datetime = Field(default_factory=utc_now)
    confidence: Confidence


# ---------- What Claude returns ----------

class FundingEvidence(StrictModel):
    """A funding event that Claude found in the supplied search snippets."""

    date: dt.date | None
    round: str | None
    url: str


class QualificationResult(StrictModel):
    """Claude's judgment of one company against the ICP (spec 5.3)."""

    business_model: BusinessModel
    sells_to: str
    icp_fit: IcpFit
    fit_reasons: list[str]
    disqualifiers: list[str]
    evidence_urls: list[str]
    funding_evidence: FundingEvidence | None
    confidence: Confidence


class Assessment(QualificationResult):
    """A stored QualificationResult, plus which company, model and time."""

    domain: str
    model: str
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)


class TitleClassification(StrictModel):
    """Claude Haiku's classification of one job title (spec 5.5)."""

    title: str                 # echoed back so batched answers can be matched up
    persona: str | None
    seniority: Seniority
    department: str
    is_match: bool
    confidence: Confidence


class TitleBatch(StrictModel):
    """Several title classifications in one Claude call (saves tokens)."""

    items: list[TitleClassification]


# ---------- Scores ----------

class AccountScore(BaseModel):
    """A company's scores and tier. `parts` explains how every number was reached."""

    domain: str
    fit_score: Score100
    intent_score: Score100
    total: Score100
    tier: Tier
    parts: dict[str, Any] = Field(default_factory=dict)
    exclusion_reason: str | None = None

    @field_validator("domain")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return normalize_domain(v)
