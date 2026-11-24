"""TheirStack: US job posts for the hiring signal, plus tech slugs from job posts (D3).

Verified against the official docs on 2026-11-24 (docs/PROVIDERS.md).
All calls go through providers.base (cache, budget, retries, throttle, log).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.models import ICP, Signal, SignalType, normalize_domain
from src.providers.base import (
    CallContext,
    CostEstimate,
    HttpRequest,
    ProviderExhausted,
    ProviderHTTPError,
    call_json,
    provider_settings,
    require_key,
)

PROVIDER = "theirstack"
BASE_URL = "https://api.theirstack.com"
KEY_ENV = "THEIRSTACK_API_KEY"


def estimate_cost(endpoint: str, limit: int = 25) -> CostEstimate:
    """Job search: 1 API credit per job returned (worst case = limit). Free plan -> $0."""
    if endpoint == "jobs/search":
        return CostEstimate(credits=float(limit), credit_bucket="api")
    return CostEstimate(credit_bucket="api")


def _jobs_actual(data: Any) -> CostEstimate:
    return CostEstimate(credits=float(len(data.get("data") or [])), credit_bucket="api")


def interpret(status: int, body: Any) -> Any:
    """Map TheirStack statuses. 402 (undocumented 'Payment Required') = out of credits."""
    if status == 200:
        meta = (body or {}).get("metadata") or {}
        if meta.get("truncated_results"):
            # Results were cut because credits ran out mid-request: keep what we got,
            # but don't call TheirStack again this run.
            body = {**body, "_credits_exhausted": True}
        return body
    err = (body or {}).get("error") or {}
    detail = f"{err.get('code', '')} {err.get('title', '')} {err.get('description', '')}".strip()
    if status == 402:
        raise ProviderExhausted(f"theirstack: HTTP 402 Payment Required (out of credits?) {detail}")
    if status == 401:
        raise ProviderHTTPError("theirstack: HTTP 401: check THEIRSTACK_API_KEY in .env", status)
    raise ProviderHTTPError(f"theirstack: HTTP {status} {detail}", status)


def build_job_search(icp: ICP, settings: dict[str, Any], page: int) -> dict[str, Any]:
    """Body for POST /v1/jobs/search: US SDR/BDR posts from the last N days, sized companies."""
    cfg = provider_settings(settings, PROVIDER).get("search") or {}
    lookback = int((settings.get("discover") or {}).get("job_lookback_days", 30))
    titles = [t.replace(",", " ") for t in icp.soft_signals.hiring_titles]  # comma -> 422
    # Widen size bounds ~20% so we don't lose companies TheirStack sizes slightly differently;
    # Prospeo and our own check apply the exact range later.
    hf = icp.hard_filters
    return {
        "job_title_or": titles,
        "job_country_code_or": ["US"],
        "company_country_code_or": ["US"],
        "posted_at_max_age_days": lookback,
        "min_employee_count": max(1, int(hf.employee_min * 0.8)),
        "max_employee_count": int(hf.employee_max * 1.2),
        "limit": int(cfg.get("page_size", 25)),
        "page": page,
    }


def search_jobs(ctx: CallContext, icp: ICP, settings: dict[str, Any],
                page: int = 0) -> dict[str, Any]:
    """One page of job search results (raw JSON). `page` is 0-based (docs)."""
    body = build_job_search(icp, settings, page)
    key = "mock-key" if ctx.mock else require_key(KEY_ENV)
    req = HttpRequest(provider=PROVIDER, endpoint="jobs/search", method="POST",
                      url=f"{BASE_URL}/v1/jobs/search", json_body=body,
                      headers={"Authorization": f"Bearer {key}"},
                      accept_status=frozenset({200, 400, 401, 402, 422}),
                      interpret=interpret, fixture_key=f"page{page}")
    data = call_json(ctx, req, estimate_cost("jobs/search", body["limit"]), actual=_jobs_actual)
    if data.get("_credits_exhausted"):
        ctx.exhausted.add(PROVIDER)
    return data


@dataclass
class HiringCompany:
    """Everything one company's job posts told us."""

    domain: str
    name: str | None = None
    employee_count: int | None = None
    country_code: str | None = None
    industry: str | None = None
    is_recruiting_agency: bool | None = None
    linkedin_url: str | None = None
    technology_slugs: set[str] = field(default_factory=set)
    funding_stage: str | None = None
    last_funding_round_date: date | None = None
    jobs: list[dict[str, Any]] = field(default_factory=list)   # {title, url, date_posted}


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def group_by_company(pages: list[dict[str, Any]]) -> dict[str, HiringCompany]:
    """Merge job results from all pages into one record per company domain."""
    companies: dict[str, HiringCompany] = {}
    for data in pages:
        for job in data.get("data") or []:
            co = job.get("company_object") or {}
            raw_domain = job.get("company_domain") or co.get("domain")
            if not raw_domain:
                continue
            try:
                domain = normalize_domain(raw_domain)
            except ValueError:
                continue
            hc = companies.setdefault(domain, HiringCompany(domain=domain))
            hc.name = hc.name or co.get("name")
            hc.employee_count = hc.employee_count or co.get("employee_count")
            hc.country_code = hc.country_code or co.get("country_code")
            hc.industry = hc.industry or co.get("industry")
            if co.get("is_recruiting_agency") is not None:
                hc.is_recruiting_agency = co.get("is_recruiting_agency")
            hc.linkedin_url = hc.linkedin_url or co.get("linkedin_url")
            hc.technology_slugs |= set(co.get("technology_slugs") or [])
            hc.technology_slugs |= set(job.get("technology_slugs") or [])
            hc.funding_stage = hc.funding_stage or co.get("funding_stage")
            hc.last_funding_round_date = hc.last_funding_round_date or _date(
                co.get("last_funding_round_date"))
            url = job.get("final_url") or job.get("url") or job.get("source_url")
            if not any(j["url"] == url for j in hc.jobs):
                hc.jobs.append({"title": job.get("job_title"), "url": url,
                                "date_posted": _date(job.get("date_posted"))})
    return companies


def hiring_signals(hc: HiringCompany, confidence: float) -> list[Signal]:
    """One hiring signal per matching job post (source_url = the job URL)."""
    return [Signal(domain=hc.domain, signal_type=SignalType.HIRING,
                   detail=j["title"] or "matching job post",
                   observed_at=datetime.combine(j["date_posted"], datetime.min.time())
                   if j["date_posted"] else None,
                   source=PROVIDER, source_url=j["url"], confidence=confidence)
            for j in hc.jobs]


def tech_signals(hc: HiringCompany, technologies: list[str], settings: dict[str, Any],
                 confidence: float) -> list[Signal]:
    """D3: tech seen in the company's job posts = medium-confidence signal; absent = unknown."""
    slugs = provider_settings(settings, PROVIDER).get("technology_slugs") or {}
    out = []
    for tech in technologies:
        slug = slugs.get(tech, tech.lower())
        if slug in hc.technology_slugs:
            out.append(Signal(domain=hc.domain, signal_type=SignalType.TECH, detail=tech,
                              source=PROVIDER, confidence=confidence,
                              source_url=hc.jobs[0]["url"] if hc.jobs else None))
    return out
