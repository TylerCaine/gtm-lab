"""Hunter: fallback email finder (after Prospeo) and email verifier.

Verified against https://hunter.io/api-documentation and https://hunter.io/openapi.json
on 2026-11-24 (docs/PROVIDERS.md). The key is sent in the X-API-KEY header, never the URL.
Note Hunter's unusual statuses: 429 = out of credits, 403 = rate limit.
"""

from dataclasses import dataclass
from typing import Any

from src.providers.base import (
    CallContext,
    CostEstimate,
    HttpRequest,
    ProviderExhausted,
    ProviderHTTPError,
    RetryableError,
    call_json,
    require_key,
)

PROVIDER = "hunter"
BASE_URL = "https://api.hunter.io/v2"
KEY_ENV = "HUNTER_API_KEY"

# Hunter verifier status -> our email_status
STATUS_MAP = {"valid": "valid", "accept_all": "catch_all", "invalid": "invalid",
              "webmail": "unknown", "disposable": "unknown", "unknown": "unknown"}


def estimate_cost(endpoint: str) -> CostEstimate:
    """Finder: 1 credit if found. Verifier: 0.5 credit. Account: free."""
    return {"email-finder": CostEstimate(credits=1.0),
            "email-verifier": CostEstimate(credits=0.5)}.get(endpoint, CostEstimate())


def _finder_actual(data: Any) -> CostEstimate:
    found = bool(((data or {}).get("data") or {}).get("email"))
    return CostEstimate(credits=1.0 if found else 0.0)


def _first_error(body: Any) -> str:
    errors = (body or {}).get("errors") or []
    if errors and isinstance(errors, list):
        e = errors[0]
        return f"{e.get('id', '')}: {e.get('details', '')}".strip(": ")
    return ""


def interpret(status: int, body: Any) -> Any:
    """Map Hunter statuses (docs) to our behavior."""
    if status == 200:
        return body
    if status in (404, 451):          # no matching profile / claimed for removal
        return {"data": {"email": None}, "_not_found": status}
    if status == 202:                 # verification still running: poll again
        raise RetryableError("hunter: verification in progress (202)", status)
    if status == 222:                 # SMTP server gave an unexpected answer: record unknown
        return {"data": {"status": "unknown"}, "_smtp_unexpected": True}
    detail = _first_error(body)
    if status == 429:
        raise ProviderExhausted(f"hunter: usage limit reached (429) {detail}")
    if status == 403:
        if "rate" in detail.lower():
            raise RetryableError(f"hunter: rate limited (403) {detail}", status)
        raise ProviderHTTPError(f"hunter: forbidden (403) {detail}", status)
    if status == 401:
        raise ProviderHTTPError("hunter: 401: check HUNTER_API_KEY in .env", status)
    raise ProviderHTTPError(f"hunter: HTTP {status} {detail}", status)


def _get(ctx: CallContext, endpoint: str, params: dict[str, Any], actual=None,
         fixture_key: str | None = None) -> Any:
    key = "mock-key" if ctx.mock else require_key(KEY_ENV)
    req = HttpRequest(provider=PROVIDER, endpoint=endpoint, method="GET",
                      url=f"{BASE_URL}/{endpoint}", params=params,
                      headers={"X-API-KEY": key},
                      accept_status=frozenset({200, 202, 222, 400, 401, 403, 404, 429, 451}),
                      interpret=interpret, fixture_key=fixture_key)
    return call_json(ctx, req, estimate_cost(endpoint), actual=actual)


@dataclass
class HunterEmail:
    email: str | None
    score: int | None
    status: str | None      # our status if Hunter's finder already verified it, else None


def find_email(ctx: CallContext, domain: str, first_name: str | None,
               last_name: str | None, full_name: str | None = None) -> HunterEmail:
    """GET /email-finder by domain + name."""
    params: dict[str, Any] = {"domain": domain, "max_duration": 10}
    if first_name and last_name:
        params.update(first_name=first_name, last_name=last_name)
    elif full_name:
        params["full_name"] = full_name
    else:
        return HunterEmail(None, None, None)
    data = _get(ctx, "email-finder", params, actual=_finder_actual,
                fixture_key=f"{domain}__{(first_name or full_name or '').lower()}")
    d = data.get("data") or {}
    email = d.get("email")
    vstatus = ((d.get("verification") or {}).get("status") or "").lower()
    return HunterEmail(email.strip().lower() if email else None, d.get("score"),
                       STATUS_MAP.get(vstatus) if vstatus == "valid" else None)


def verify_email(ctx: CallContext, email: str) -> str:
    """GET /email-verifier. Returns our status: valid / catch_all / invalid / unknown."""
    data = _get(ctx, "email-verifier", {"email": email}, fixture_key=email)
    status = str(((data.get("data") or {}).get("status") or "unknown")).lower()
    return STATUS_MAP.get(status, "unknown")


def account(ctx: CallContext) -> dict[str, Any]:
    """GET /account (free, never cached): remaining credits."""
    key = "mock-key" if ctx.mock else require_key(KEY_ENV)
    req = HttpRequest(provider=PROVIDER, endpoint="account", method="GET",
                      url=f"{BASE_URL}/account", headers={"X-API-KEY": key},
                      accept_status=frozenset({200, 401, 403, 429}), interpret=interpret,
                      cacheable=False)
    return call_json(ctx, req, estimate_cost("account")).get("data") or {}
