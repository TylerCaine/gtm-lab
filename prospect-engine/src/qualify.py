"""Qualify (spec 5.3): public page text + funding evidence -> Claude ICP assessment.

Claude may only use the supplied text. Every evidence URL it returns is checked
against the URLs we actually supplied; unknown URLs are dropped and confidence lowered.
"""

import logging
from typing import Any

from src import db
from src.models import ICP, Assessment, QualificationResult, Signal, SignalType
from src.providers import claude, tavily
from src.providers.base import BudgetExceeded, CallContext, ProviderError

log = logging.getLogger(__name__)

SYSTEM = """You assess whether a company fits an Ideal Customer Profile (ICP) for B2B sales prospecting.

Rules:
- Use ONLY the text supplied in the user message. Do not use outside knowledge about the company.
- If the supplied text is not enough to judge, say so in fit_reasons, lower confidence, and prefer "weak" over guessing.
- evidence_urls must be copied exactly from the SUPPLIED URLS list. Never invent or alter URLs.
- funding_evidence: only fill it if a supplied snippet clearly describes a funding round inside the stated window; its url must be one of the SUPPLIED URLS. Otherwise null.
- business_model describes how the company itself makes money (b2b_saas = sells software subscriptions to businesses).
- confidence is 0 to 1: how sure you are of icp_fit given the evidence."""

MISSING_PAGE_NOTE = "(no page text could be extracted)"


def page_urls(domain: str, settings: dict[str, Any]) -> list[str]:
    paths = (settings.get("qualify") or {}).get("page_paths") or ["/"]
    n = int((settings.get("qualify") or {}).get("max_pages_per_company", 3))
    return [f"https://{domain}{p}" for p in paths[:n]]


def build_prompt(icp: ICP, domain: str, name: str, facts: dict[str, Any],
                 pages: dict[str, str], signals: list[Signal], funding_results: list[dict],
                 max_chars: int, window_days: int) -> tuple[str, set[str]]:
    """Build the user message and the set of URLs Claude is allowed to cite."""
    allowed: set[str] = set()
    hf = icp.hard_filters
    lines = [
        "ICP:",
        f"- Business model: {hf.business_model}; country: {hf.country}; "
        f"employees {hf.employee_min}-{hf.employee_max}",
        f"- Description: {icp.description}",
        "",
        f"COMPANY: {name} ({domain})",
        *(f"- {k}: {v}" for k, v in facts.items() if v is not None),
        "",
        "SIGNALS FROM DATA PROVIDERS:",
    ]
    for s in signals:
        when = s.observed_at.date().isoformat() if s.observed_at else "date unknown"
        lines.append(f"- [{s.signal_type}] {s.detail} ({s.source}, {when})"
                     + (f" {s.source_url}" if s.source_url else ""))
        if s.source_url:
            allowed.add(s.source_url)
    if not signals:
        lines.append("- none")

    lines += ["", f"FUNDING NEWS SNIPPETS (window: last {window_days} days):"]
    for r in funding_results:
        lines.append(f"- {r.get('title')} | {r.get('published_date') or 'date unknown'} | "
                     f"{r['url']}\n  {(r.get('content') or '')[:600]}")
        allowed.add(r["url"])
    if not funding_results:
        lines.append("- none")

    lines += ["", "PUBLIC PAGE TEXT:"]
    per_page = max_chars // max(len(pages), 1)
    for url, text in pages.items():
        allowed.add(url)
        lines.append(f"--- {url} ---\n{(text or MISSING_PAGE_NOTE)[:per_page]}")
    if not pages:
        lines.append(MISSING_PAGE_NOTE)

    lines += ["", "SUPPLIED URLS (the only URLs you may cite):", *sorted(allowed)]
    return "\n".join(lines), allowed


def validate_evidence(result: QualificationResult, allowed: set[str]) -> QualificationResult:
    """Drop evidence URLs that weren't supplied; lower confidence if anything was dropped."""
    kept = [u for u in result.evidence_urls if u in allowed]
    dropped = len(kept) != len(result.evidence_urls)
    funding = result.funding_evidence
    if funding is not None and funding.url not in allowed:
        funding, dropped = None, True
    if not dropped:
        return result
    return result.model_copy(update={"evidence_urls": kept, "funding_evidence": funding,
                                     "confidence": round(result.confidence * 0.75, 3)})


def qualify(ctx: CallContext, icp: ICP, settings: dict[str, Any], scoring: dict[str, Any],
            domains: list[str], names: dict[str, str]) -> dict[str, str]:
    """Assess each domain. Returns {domain: failure reason} for ones that couldn't be assessed."""
    q = settings.get("qualify") or {}
    max_chars = int(q.get("max_page_tokens", 6000)) * 4
    window = int(q.get("funding_window_days", 180))
    mode = q.get("funding_search", "if_missing")
    model = (settings.get("models") or {}).get("qualify", "claude-sonnet-5-5")
    conf = scoring["confidence_values"]
    failures: dict[str, str] = {}

    for domain in domains:
        company = db.get_company(ctx.conn, domain)
        name = (company.name if company else None) or names.get(domain) or domain
        signals = db.get_signals(ctx.conn, domain)

        pages: dict[str, str] = {}
        try:
            pages = tavily.extract(ctx, page_urls(domain, settings), fixture_key=domain)["pages"]
        except (BudgetExceeded, ProviderError) as exc:
            log.warning("Page extraction failed for %s: %s", domain, exc)

        funding_results: list[dict] = []
        has_dated_funding = any(s.signal_type == SignalType.FUNDING for s in signals)
        if mode == "always" or (mode == "if_missing" and not has_dated_funding):
            try:
                funding_results = tavily.funding_search(ctx, name, window, ctx.now().date(),
                                                        fixture_key=domain)
            except (BudgetExceeded, ProviderError) as exc:
                log.warning("Funding search failed for %s: %s", domain, exc)

        facts = {"employee_count": company.employee_count if company else None,
                 "industry": company.industry if company else None,
                 "country": company.country if company else None}
        user, allowed = build_prompt(icp, domain, name, facts, pages, signals,
                                     funding_results, max_chars, window)
        try:
            answer = claude.structured_answer(ctx, task="qualify", model=model, system=SYSTEM,
                                              user=user, schema=QualificationResult,
                                              fixture_key=domain)
        except (BudgetExceeded, ProviderError) as exc:
            failures[domain] = f"qualification failed: {exc}"
            log.warning("Qualification failed for %s: %s", domain, exc)
            continue

        result = validate_evidence(answer.output, allowed)
        # Record the model that actually answered (differs from `model` if the
        # server-side refusal fallback served the request).
        db.upsert_assessment(ctx.conn, Assessment(**result.model_dump(), domain=domain,
                                                  model=answer.model, created_at=ctx.now()))
        fe = result.funding_evidence
        if fe is not None:
            # Funding is the least reliable signal: never more than "low-medium" confidence.
            db.add_signal(ctx.conn, Signal(
                domain=domain, signal_type=SignalType.FUNDING,
                detail=f"{fe.round or 'funding round'} (from news, judged by Claude)",
                observed_at=None, source="tavily+claude", source_url=fe.url,
                confidence=min(result.confidence, conf["low"])), now=ctx.now())
    return failures
