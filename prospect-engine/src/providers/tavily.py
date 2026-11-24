"""Tavily: public page text (extract) and funding-news search.

Verified against docs.tavily.com on 2026-11-24 (docs/PROVIDERS.md). D1: this is the
only way the tool reads company websites; we never fetch sites ourselves.
"""

import math
from datetime import date, timedelta
from typing import Any

from src.providers.base import (
    CallContext,
    CostEstimate,
    HttpRequest,
    ProviderExhausted,
    ProviderHTTPError,
    call_json,
    require_key,
)

PROVIDER = "tavily"
BASE_URL = "https://api.tavily.com"
KEY_ENV = "TAVILY_API_KEY"
USD_PER_CREDIT = 0.008   # pay-as-you-go price (docs); free plan logs $0
MAX_EXTRACT_URLS = 20


def estimate_cost(endpoint: str, n_urls: int = 1) -> CostEstimate:
    """Basic search: 1 credit. Basic extract: 1 credit per 5 successful URLs (rounded up)."""
    if endpoint == "search":
        return CostEstimate(usd=USD_PER_CREDIT, credits=1.0)
    if endpoint == "extract":
        credits = float(math.ceil(n_urls / 5))
        return CostEstimate(usd=USD_PER_CREDIT * credits, credits=credits)
    return CostEstimate()


def _actual(data: Any) -> CostEstimate:
    """Use Tavily's own usage.credits when present (requested via include_usage)."""
    credits = float(((data or {}).get("usage") or {}).get("credits") or 0)
    return CostEstimate(usd=USD_PER_CREDIT * credits, credits=credits)


def interpret(status: int, body: Any) -> Any:
    if status == 200:
        return body
    detail = str((body or {}).get("detail") or "")[:200]
    if status in (432, 433):
        raise ProviderExhausted(f"tavily: HTTP {status} plan/credit limit exceeded {detail}")
    if status == 401:
        raise ProviderHTTPError("tavily: 401: check TAVILY_API_KEY in .env", status)
    raise ProviderHTTPError(f"tavily: HTTP {status} {detail}", status)


def _post(ctx: CallContext, endpoint: str, body: dict[str, Any], estimate: CostEstimate,
          fixture_key: str | None) -> Any:
    key = "mock-key" if ctx.mock else require_key(KEY_ENV)
    req = HttpRequest(provider=PROVIDER, endpoint=endpoint, method="POST",
                      url=f"{BASE_URL}/{endpoint}", json_body=body,
                      headers={"Authorization": f"Bearer {key}"},
                      accept_status=frozenset({200, 400, 401, 422, 432, 433}),
                      interpret=interpret, fixture_key=fixture_key)
    return call_json(ctx, req, estimate, actual=_actual)


def extract(ctx: CallContext, urls: list[str], fixture_key: str | None = None) -> dict[str, Any]:
    """POST /extract for up to 20 public URLs; returns {url: markdown} for successes."""
    urls = urls[:MAX_EXTRACT_URLS]
    body = {"urls": urls, "extract_depth": "basic", "format": "markdown", "include_usage": True}
    data = _post(ctx, "extract", body, estimate_cost("extract", len(urls)), fixture_key)
    pages = {r.get("url"): r.get("raw_content") or "" for r in data.get("results") or []
             if r.get("url")}
    return {"pages": pages, "failed": data.get("failed_results") or []}


def funding_search(ctx: CallContext, company_name: str, window_days: int, today: date,
                   fixture_key: str | None = None) -> list[dict[str, Any]]:
    """POST /search for recent funding news; returns results [{title, url, content, published_date}]."""
    start = (today - timedelta(days=window_days)).isoformat()
    body = {
        "query": f'"{company_name}" raised funding round',
        "search_depth": "basic",
        "topic": "news",
        "start_date": start,
        "max_results": 5,
        "include_usage": True,
    }
    data = _post(ctx, "search", body, estimate_cost("search"), fixture_key)
    return [{"title": r.get("title"), "url": r.get("url"), "content": r.get("content"),
             "published_date": r.get("published_date")}
            for r in data.get("results") or [] if r.get("url")]
