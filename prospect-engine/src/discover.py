"""Discover (spec 5.2, with D3/D10): hiring signal first, then firmographic filtering.

1. TheirStack: US job posts matching the ICP hiring titles -> hiring + tech signals.
2. Prospeo search-company limited to those domains + US / headcount / SaaS filters.
3. Our own hard-filter check (US, exact employee range, exclusions, no recruiting agencies).
Stops when target_accounts x candidate_multiplier companies pass, or budget/pages run out.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src import db
from src.models import ICP, Company, FieldValue, Signal, SignalType
from src.providers import prospeo, theirstack
from src.providers.base import BudgetExceeded, CallContext, ProviderError, provider_settings

log = logging.getLogger(__name__)


@dataclass
class DiscoverResult:
    candidates: list[str] = field(default_factory=list)          # domains passing hard filters
    excluded: dict[str, str] = field(default_factory=dict)       # domain -> reason
    hiring_companies: int = 0
    names: dict[str, str] = field(default_factory=dict)          # domain -> company name


def hard_filter_reason(company: Company, icp: ICP,
                       hiring: theirstack.HiringCompany | None) -> str | None:
    """Return why a company fails the hard filters, or None if it passes."""
    hf = icp.hard_filters
    if (company.country or "").upper() != "US":
        return f"not US (country={company.country or 'unknown'})"
    if company.employee_count is None:
        return "employee count unknown"
    if not hf.employee_min <= company.employee_count <= hf.employee_max:
        return f"{company.employee_count} employees outside {hf.employee_min}-{hf.employee_max}"
    if company.domain in icp.exclusions.domains:
        return "domain on ICP exclusion list"
    industry = (company.industry or "").lower()
    if industry and industry in icp.exclusions.industries:
        return f"excluded industry: {company.industry}"
    if hiring and hiring.is_recruiting_agency:
        return "recruiting agency (job posts are for clients)"
    return None


def _record_company_fields(ctx: CallContext, domain: str, fields: dict[str, Any],
                           source: str, confidence: float) -> None:
    for name, value in fields.items():
        if value is not None:
            db.add_field_value(ctx.conn, FieldValue(entity_type="company", entity_id=domain,
                                                    field=name, value=value, source=source,
                                                    retrieved_at=ctx.now(), confidence=confidence))


def _funding_signal(domain: str, when, stage: str | None, url: str | None, source: str,
                    confidence: float, window_days: int, today) -> Signal | None:
    """A funding signal from structured data, only if inside the funding window."""
    if when is None or (today - when) > timedelta(days=window_days) or when > today:
        return None
    return Signal(domain=domain, signal_type=SignalType.FUNDING,
                  detail=f"{stage or 'funding round'} on {when.isoformat()}",
                  observed_at=datetime.combine(when, datetime.min.time()),
                  source=source, source_url=url, confidence=confidence)


def discover(ctx: CallContext, icp: ICP, settings: dict[str, Any], scoring: dict[str, Any],
             target_accounts: int) -> DiscoverResult:
    """Run discovery and store companies, signals and provenance. Never raises ProviderError."""
    result = DiscoverResult()
    conf = scoring["confidence_values"]
    want = target_accounts * int((settings.get("discover") or {}).get("candidate_multiplier", 2))
    window = int((settings.get("qualify") or {}).get("funding_window_days", 180))
    today = ctx.now().date()

    # ---- 1. TheirStack job posts ----
    ts_cfg = provider_settings(settings, "theirstack").get("search") or {}
    page_size = int(ts_cfg.get("page_size", 25))
    pages: list[dict[str, Any]] = []
    for page in range(int(ts_cfg.get("max_pages", 4))):
        if "theirstack" in ctx.exhausted:
            break
        try:
            data = theirstack.search_jobs(ctx, icp, settings, page)
        except BudgetExceeded as exc:
            log.warning("TheirStack stopped by budget guard: %s", exc)
            break
        except ProviderError as exc:
            log.warning("TheirStack job search failed on page %d: %s", page, exc)
            break
        pages.append(data)
        hiring = theirstack.group_by_company(pages)
        if len(data.get("data") or []) < page_size or len(hiring) >= want * 2:
            break
    hiring = theirstack.group_by_company(pages)
    result.hiring_companies = len(hiring)
    log.info("TheirStack: %d companies with matching US job posts", len(hiring))

    for hc in hiring.values():
        for sig in theirstack.hiring_signals(hc, conf["high"]):
            db.add_signal(ctx.conn, sig, now=ctx.now())
        for sig in theirstack.tech_signals(hc, icp.soft_signals.technologies, settings,
                                           conf["medium"]):
            db.add_signal(ctx.conn, sig, now=ctx.now())
        _record_company_fields(ctx, hc.domain, {
            "name": hc.name, "employee_count": hc.employee_count, "country": hc.country_code,
            "industry": hc.industry, "is_recruiting_agency": hc.is_recruiting_agency,
            "technology_slugs": sorted(hc.technology_slugs) or None,
            "funding_stage": hc.funding_stage,
            "last_funding_round_date": hc.last_funding_round_date.isoformat()
            if hc.last_funding_round_date else None,
        }, "theirstack", conf["medium"])
        sig = _funding_signal(hc.domain, hc.last_funding_round_date, hc.funding_stage, None,
                              "theirstack", conf["medium"], window, today)
        if sig:
            db.add_signal(ctx.conn, sig, now=ctx.now())

    if not hiring:
        return result

    # ---- 2. Prospeo firmographic filter on the hiring domains ----
    p_cfg = provider_settings(settings, "prospeo").get("search") or {}
    domains = sorted(hiring)
    seen: set[str] = set()
    for start in range(0, len(domains), prospeo.MAX_WEBSITES_PER_SEARCH):
        chunk = domains[start:start + prospeo.MAX_WEBSITES_PER_SEARCH]
        for page in range(1, int(p_cfg.get("max_company_pages", 4)) + 1):
            if "prospeo" in ctx.exhausted or len(result.candidates) >= want:
                break
            try:
                data = prospeo.search_companies(ctx, icp, settings, page, domains=chunk)
            except BudgetExceeded as exc:
                log.warning("Prospeo stopped by budget guard: %s", exc)
                break
            except ProviderError as exc:
                log.warning("Prospeo company search failed: %s", exc)
                break
            for item in data.get("results") or []:
                parsed = prospeo.parse_company(item.get("company") or {})
                if parsed is None or parsed.company.domain in seen:
                    continue
                seen.add(parsed.company.domain)
                _store_prospeo_company(ctx, parsed, icp, conf, window, today)
                hc = hiring.get(parsed.company.domain)
                company = db.get_company(ctx.conn, parsed.company.domain) or parsed.company
                result.names[company.domain] = company.name or company.domain
                reason = hard_filter_reason(company, icp, hc)
                if reason:
                    result.excluded[company.domain] = reason
                elif len(result.candidates) < want:
                    result.candidates.append(company.domain)
            total_pages = int((data.get("pagination") or {}).get("total_page") or 1)
            if page >= total_pages:
                break

    for domain, hc in hiring.items():
        result.names.setdefault(domain, hc.name or domain)
        if domain not in seen:
            result.excluded.setdefault(
                domain, "not returned by Prospeo firmographic filters (US, size, SaaS)")
    log.info("Discovery: %d candidates passed hard filters, %d excluded",
             len(result.candidates), len(result.excluded))
    return result


def _store_prospeo_company(ctx: CallContext, parsed: prospeo.ProspeoCompany, icp: ICP,
                           conf: dict[str, float], window: int, today) -> None:
    """Upsert the company, its provenance, and Prospeo tech/funding signals."""
    db.upsert_company(ctx.conn, parsed.company, now=ctx.now())
    _record_company_fields(ctx, parsed.company.domain, parsed.fields, "prospeo", conf["high"])
    for sig in prospeo.tech_signals(parsed, icp.soft_signals.technologies, conf["medium"]):
        db.add_signal(ctx.conn, sig, now=ctx.now())
    sig = _funding_signal(parsed.company.domain, parsed.latest_funding_date,
                          parsed.latest_funding_stage, parsed.funding_link, "prospeo",
                          conf["medium"], window, today)
    if sig:
        db.add_signal(ctx.conn, sig, now=ctx.now())
