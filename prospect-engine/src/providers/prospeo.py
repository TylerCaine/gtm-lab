"""Prospeo: company search, person search, email enrichment (D10, replaces PDL).

Every endpoint, filter and field name here was verified against the official docs
on 2026-11-24. See docs/PROVIDERS.md for sources. Nothing is guessed: fields the
docs don't promise (e.g. technology data in search results) are treated as
optional and "unknown" when missing.

All calls go through providers.base (cache, budget, retries, throttle, log).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.models import ICP, Company, Seniority, Signal, SignalType, normalize_domain
from src.providers.base import (
    CallContext,
    CostEstimate,
    HttpRequest,
    ProviderExhausted,
    ProviderHTTPError,
    RetryableError,
    call_json,
    provider_settings,
    require_key,
)

PROVIDER = "prospeo"
BASE_URL = "https://api.prospeo.io"
KEY_ENV = "PROSPEO_API_KEY"

# Our ICP seniorities -> Prospeo's exact enum values (api-docs/enum/seniorities).
SENIORITY_TO_PROSPEO = {
    Seniority.C_SUITE: "C-Suite",
    Seniority.VP: "Vice President",
    Seniority.HEAD: "Head",
    Seniority.DIRECTOR: "Director",
    Seniority.MANAGER: "Manager",
}
# Prospeo seniority -> ours (for classifying returned people).
PROSPEO_TO_SENIORITY = {
    "Founder/Owner": Seniority.C_SUITE,
    "C-Suite": Seniority.C_SUITE,
    "Partner": Seniority.C_SUITE,
    "Vice President": Seniority.VP,
    "Head": Seniority.HEAD,
    "Director": Seniority.DIRECTOR,
    "Manager": Seniority.MANAGER,
    "Senior": Seniority.INDIVIDUAL_CONTRIBUTOR,
    "Entry": Seniority.INDIVIDUAL_CONTRIBUTOR,
    "Intern": Seniority.INDIVIDUAL_CONTRIBUTOR,
}

NO_RESULT_CODES = {"NO_MATCH", "NO_RESULTS"}
RETRY_CODES = {"SERVICE_TEMPORARILY_UNAVAILABLE"}
MAX_WEBSITES_PER_SEARCH = 500   # docs: names + websites max 500 combined
MAX_TITLES = 100                # docs: person_job_title include max 100


# ---------- cost ----------

def estimate_cost(endpoint: str) -> CostEstimate:
    """Worst-case credits for one call (docs: search 1/page with results; email 1 if found).

    Dollar cost is 0: paid-plan per-credit price couldn't be verified (pricing page
    unreadable), and on the free plan credits are what's limited.
    """
    if endpoint in ("search-company", "search-person", "enrich-person"):
        return CostEstimate(usd=0.0, credits=1.0)
    return CostEstimate()  # search-suggestions, account-information: free


def _search_actual(data: Any) -> CostEstimate:
    """Charged only when results came back and the page wasn't a free 30-day repeat."""
    charged = bool(data.get("results")) and not data.get("free", False)
    return CostEstimate(credits=1.0 if charged else 0.0)


def _enrich_actual(data: Any) -> CostEstimate:
    """Charged only when an email was found and it wasn't a free re-enrichment."""
    email = ((data.get("person") or {}).get("email") or {})
    found = bool(email.get("email")) and email.get("status") == "VERIFIED"
    charged = found and not data.get("free_enrichment", False)
    return CostEstimate(credits=1.0 if charged else 0.0)


# ---------- request plumbing ----------

def _headers(ctx: CallContext) -> dict[str, str]:
    """Auth headers. In mock mode no key is needed."""
    key = "mock-key" if ctx.mock else require_key(KEY_ENV)
    return {"X-KEY": key, "Content-Type": "application/json"}


def interpret(status: int, body: Any) -> Any:
    """Turn Prospeo's error_code into our behavior (docs: all API errors are HTTP 400)."""
    if isinstance(body, dict) and body.get("error"):
        code = str(body.get("error_code") or "UNKNOWN")
        detail = body.get("filter_error") or ""
        if code in NO_RESULT_CODES:
            return {"error": False, "no_match": True, "error_code": code, "results": []}
        if code in RETRY_CODES:
            raise RetryableError(f"prospeo: {code}", status)
        if code == "INSUFFICIENT_CREDITS":
            raise ProviderExhausted("prospeo: INSUFFICIENT_CREDITS (account is out of credits)")
        if code == "PLAN_REQUIRED":
            raise ProviderHTTPError(
                f"prospeo: PLAN_REQUIRED: a filter needs a higher plan ({detail}). "
                "Turn it off in config/settings.yaml (e.g. use_technology_filter: false).", status)
        if code == "INVALID_API_KEY":
            raise ProviderHTTPError("prospeo: INVALID_API_KEY: check PROSPEO_API_KEY in .env", status)
        raise ProviderHTTPError(f"prospeo: {code} {detail}".strip(), status)
    if status != 200:
        raise ProviderHTTPError(f"prospeo: HTTP {status}", status)
    return body


def _post(ctx: CallContext, endpoint: str, body: dict[str, Any], actual=None,
          fixture_key: str | None = None) -> Any:
    req = HttpRequest(provider=PROVIDER, endpoint=endpoint, method="POST",
                      url=f"{BASE_URL}/{endpoint}", json_body=body, headers=_headers(ctx),
                      accept_status=frozenset({200, 400}), interpret=interpret,
                      fixture_key=fixture_key)
    return call_json(ctx, req, estimate_cost(endpoint), actual=actual)


def _search_cfg(settings: dict[str, Any]) -> dict[str, Any]:
    return provider_settings(settings, PROVIDER).get("search") or {}


# ---------- company search ----------

def build_company_filters(icp: ICP, settings: dict[str, Any],
                          domains: list[str] | None = None) -> dict[str, Any]:
    """Filters for /search-company from the ICP hard filters + settings (see PROVIDERS.md).

    `domains` (optional, max 500) limits the search to these companies, e.g. the ones
    TheirStack found hiring SDRs.
    """
    cfg = _search_cfg(settings)
    hf = icp.hard_filters
    filters: dict[str, Any] = {
        "company_location_search": {"include": [cfg.get("location", "United States")]},
    }
    if domains:
        if len(domains) > MAX_WEBSITES_PER_SEARCH:
            raise ValueError(f"at most {MAX_WEBSITES_PER_SEARCH} domains per company search")
        filters["company"] = {"websites": {"include": sorted(set(domains))}}
    if cfg.get("headcount_mode", "custom") == "ranges":
        filters["company_headcount_range"] = headcount_ranges(hf.employee_min, hf.employee_max)
    else:
        filters["company_headcount_custom"] = {"min": hf.employee_min, "max": hf.employee_max}
    if cfg.get("company_subtypes"):
        filters["company_type"] = {"subtypes": {"include": list(cfg["company_subtypes"])}}
    if cfg.get("company_industries"):
        filters["company_industry"] = {"include": list(cfg["company_industries"])}
    if cfg.get("use_technology_filter") and icp.soft_signals.technologies:
        filters["company_technology"] = {"include": icp.soft_signals.technologies[:20]}
    return filters


HEADCOUNT_BUCKETS = [(1, 10), (11, 20), (21, 50), (51, 100), (101, 200), (201, 500),
                     (501, 1000), (1001, 2000), (2001, 5000), (5001, 10000)]


def headcount_ranges(lo: int, hi: int) -> list[str]:
    """Prospeo buckets overlapping [lo, hi] (docs enum). Includes '10000+' if hi > 10000."""
    out = [f"{a}-{b}" for a, b in HEADCOUNT_BUCKETS if a <= hi and b >= lo]
    if hi > 10000:
        out.append("10000+")
    return out


def search_companies(ctx: CallContext, icp: ICP, settings: dict[str, Any],
                     page: int = 1, domains: list[str] | None = None) -> dict[str, Any]:
    """One page (up to 25) of /search-company results. Raw JSON."""
    body = {"page": page, "filters": build_company_filters(icp, settings, domains)}
    return _post(ctx, "search-company", body, actual=_search_actual,
                 fixture_key=f"page{page}")


@dataclass
class ProspeoCompany:
    """A parsed company result plus the optional signals Prospeo may include."""

    company: Company
    company_id: str | None = None
    technologies: list[str] | None = None        # None = not present in the response
    is_b2b: bool | None = None
    latest_funding_date: date | None = None
    latest_funding_stage: str | None = None
    funding_link: str | None = None
    active_job_titles: list[str] | None = None
    fields: dict[str, Any] = field(default_factory=dict)  # everything, for provenance


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_company(raw: dict[str, Any]) -> ProspeoCompany | None:
    """Parse a Prospeo company object. Returns None if it has no usable domain."""
    if not raw:
        return None
    domain_src = raw.get("domain") or raw.get("website")
    if not domain_src:
        return None
    try:
        domain = normalize_domain(domain_src)
    except ValueError:
        return None
    loc = raw.get("location") or {}
    company = Company(domain=domain, name=raw.get("name"),
                      employee_count=raw.get("employee_count"),
                      country=loc.get("country_code"), industry=raw.get("industry"),
                      linkedin_url=raw.get("linkedin_url"))
    tech = raw.get("technology")
    technologies = None
    if isinstance(tech, dict) and tech.get("technology_names") is not None:
        technologies = [str(t) for t in tech["technology_names"]]
    funding = raw.get("funding") or {}
    events = funding.get("funding_events") or []
    latest_link = None
    if events:
        newest = max(events, key=lambda e: str(e.get("raised_at") or ""))
        latest_link = newest.get("link")
    jobs = raw.get("job_postings")
    return ProspeoCompany(
        company=company,
        company_id=raw.get("company_id"),
        technologies=technologies,
        is_b2b=(raw.get("attributes") or {}).get("is_b2b"),
        latest_funding_date=_parse_date(funding.get("latest_funding_date")),
        latest_funding_stage=funding.get("latest_funding_stage"),
        funding_link=latest_link,
        active_job_titles=jobs.get("active_titles") if isinstance(jobs, dict) else None,
        fields={
            "name": raw.get("name"), "employee_count": raw.get("employee_count"),
            "employee_range": raw.get("employee_range"), "industry": raw.get("industry"),
            "country": loc.get("country_code"), "type": raw.get("type"),
            "company_id": raw.get("company_id"), "is_b2b": (raw.get("attributes") or {}).get("is_b2b"),
            "technology_names": technologies,
            "latest_funding_date": funding.get("latest_funding_date"),
            "latest_funding_stage": funding.get("latest_funding_stage"),
        },
    )


def tech_signals(parsed: ProspeoCompany, technologies: list[str],
                 confidence: float, source_url: str | None = None) -> list[Signal]:
    """D10: a Prospeo technology match is an independent tech signal (medium confidence).

    No match, or no technology data at all, produces no signal = "unknown", never "no".
    """
    if not parsed.technologies:
        return []
    have = {t.lower() for t in parsed.technologies}
    return [Signal(domain=parsed.company.domain, signal_type=SignalType.TECH, detail=tech,
                   source=PROVIDER, source_url=source_url, confidence=confidence)
            for tech in technologies if tech.lower() in have]


# ---------- person search ----------

def job_titles(icp: ICP) -> list[str]:
    """All persona titles, de-duplicated (case-insensitive), max 100 (docs limit)."""
    seen: dict[str, str] = {}
    for persona in sorted(icp.personas, key=lambda p: p.priority):
        for t in persona.titles:
            seen.setdefault(t.lower(), t)
    return list(seen.values())[:MAX_TITLES]


def seniorities(icp: ICP) -> list[str]:
    """Prospeo seniority values for all personas (unmappable ones are skipped)."""
    out: list[str] = []
    for persona in icp.personas:
        for s in persona.seniorities:
            value = SENIORITY_TO_PROSPEO.get(s)
            if value and value not in out:
                out.append(value)
    return out


def build_person_filters(domains: list[str], icp: ICP,
                         settings: dict[str, Any]) -> dict[str, Any]:
    """Filters for /search-person: these companies, these titles/seniorities, in the US."""
    cfg = _search_cfg(settings)
    filters: dict[str, Any] = {
        "company": {"websites": {"include": domains}},
        "person_job_title": {"include": job_titles(icp), "match_mode": "CONTAINS"},
        "person_location_search": {"include": [cfg.get("location", "United States")]},
        "max_person_per_company": int(cfg.get("max_person_per_company", 5)),
    }
    levels = seniorities(icp)
    if levels:
        filters["person_seniority"] = {"include": levels}
    return filters


def search_people(ctx: CallContext, domains: list[str], icp: ICP, settings: dict[str, Any],
                  page: int = 1) -> dict[str, Any]:
    """One page (up to 25) of /search-person results for up to 500 domains. Raw JSON."""
    if len(domains) > MAX_WEBSITES_PER_SEARCH:
        raise ValueError(f"at most {MAX_WEBSITES_PER_SEARCH} domains per person search")
    domains = sorted({normalize_domain(d) for d in domains})
    body = {"page": page, "filters": build_person_filters(domains, icp, settings)}
    return _post(ctx, "search-person", body, actual=_search_actual, fixture_key=f"page{page}")


@dataclass
class ProspeoPerson:
    """A parsed person result (no email: search never reveals it)."""

    person_id: str
    domain: str | None
    full_name: str
    first_name: str | None
    last_name: str | None
    title: str | None
    seniority: Seniority | None
    prospeo_seniority: str | None
    departments: list[str]
    country_code: str | None
    linkedin_url: str | None


def parse_person(result: dict[str, Any]) -> ProspeoPerson | None:
    """Parse one search-person result ({person, company}). None if unusable."""
    person = result.get("person") or {}
    pid = person.get("person_id")
    name = person.get("full_name") or " ".join(
        x for x in (person.get("first_name"), person.get("last_name")) if x)
    if not pid or not name:
        return None
    company = result.get("company") or {}
    domain = None
    if company.get("domain") or company.get("website"):
        try:
            domain = normalize_domain(company.get("domain") or company.get("website"))
        except ValueError:
            domain = None
    # Seniority/departments only exist inside job_history (person-object docs).
    current = next((j for j in (person.get("job_history") or []) if j.get("current")), {})
    p_sen = current.get("seniority")
    return ProspeoPerson(
        person_id=str(pid), domain=domain, full_name=name,
        first_name=person.get("first_name"), last_name=person.get("last_name"),
        title=person.get("current_job_title") or current.get("title"),
        seniority=PROSPEO_TO_SENIORITY.get(p_sen) if p_sen else None,
        prospeo_seniority=p_sen, departments=list(current.get("departments") or []),
        country_code=(person.get("location") or {}).get("country_code"),
        linkedin_url=person.get("linkedin_url"),
    )


# ---------- email enrichment ----------

@dataclass
class EmailResult:
    """Outcome of enrich-person. `email` is set only when Prospeo verified it."""

    email: str | None
    status: str | None          # our status: "valid" or None (= not found, try Hunter)
    prospeo_status: str | None
    verification_method: str | None


def enrich_email(ctx: CallContext, person_id: str, settings: dict[str, Any]) -> EmailResult:
    """Reveal a person's work email via /enrich-person using the search person_id."""
    cfg = provider_settings(settings, PROVIDER).get("enrich") or {}
    body = {
        "only_verified_email": bool(cfg.get("only_verified_email", False)),
        "enrich_mobile": False,  # never: 10 credits + calling compliance
        "data": {"person_id": person_id},
    }
    data = _post(ctx, "enrich-person", body, actual=_enrich_actual, fixture_key=person_id)
    email = ((data.get("person") or {}).get("email") or {})
    p_status = email.get("status")
    address = email.get("email")
    if p_status == "VERIFIED" and address and "*" not in address:
        return EmailResult(address.strip().lower(), "valid", p_status,
                           email.get("verification_method"))
    return EmailResult(None, None, p_status, email.get("verification_method"))


# ---------- free helpers ----------

def suggestions(ctx: CallContext, field_name: str, query: str) -> Any:
    """Canonical filter values from /search-suggestions (free). e.g. ('technology_search', 'hubspot')."""
    return _post(ctx, "search-suggestions", {field_name: query})


def account_information(ctx: CallContext) -> dict[str, Any]:
    """Live plan and credit balance from GET /account-information (free, never cached)."""
    req = HttpRequest(provider=PROVIDER, endpoint="account-information", method="GET",
                      url=f"{BASE_URL}/account-information", headers=_headers(ctx),
                      accept_status=frozenset({200, 400}), interpret=interpret,
                      cacheable=False)
    return call_json(ctx, req, estimate_cost("account-information")).get("response") or {}
