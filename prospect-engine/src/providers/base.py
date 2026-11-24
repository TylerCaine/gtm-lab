"""The gatekeeper for every paid API call (spec rule 10).

Provider modules never call the network themselves. They describe a request
and its estimated cost, and `guarded_call` (or `call_json` for plain HTTP) does:

1. fingerprint the request (sorted params, never headers or API keys);
2. mock mode: return a fixture from tests/fixtures/ (no network, $0);
3. cache: return a fresh-enough saved answer (logged as cached, $0);
4. budget: refuse if run budget, monthly cap or free-tier credit cap would be exceeded;
5. send with timeout + exponential-backoff retries on 429/5xx/network errors;
6. log the call to api_calls (also on failure) and cache successful answers.

Failures raise ProviderError so the calling stage can mark that record and continue.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from src import db
from src.config import PROJECT_ROOT
from src.models import utc_now

log = logging.getLogger(__name__)
# httpx logs full request URLs at INFO. Some APIs take keys in the URL, so keep it quiet (rule 7).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

USER_AGENT = "ProspectEngine/0.1 (learning project)"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


# ---------- errors ----------

class MissingApiKey(Exception):
    """An API key environment variable is not set. Stops the run with a clear message."""


class ProviderError(Exception):
    """A provider call failed. The stage should mark the record and carry on."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RetryableError(ProviderError):
    """A temporary failure (429, 5xx, timeout, network) worth retrying."""


class ProviderHTTPError(ProviderError):
    """A non-retryable HTTP error such as 400, 401, 403."""


class BudgetExceeded(ProviderError):
    """The call was refused because it would break a budget or credit cap."""


class ProviderExhausted(BudgetExceeded):
    """The provider says the account is out of credits. No more calls to it this run."""


class MockFixtureMissing(ProviderError):
    """--mock was used but no fixture file exists for this call."""


# ---------- small data types ----------

@dataclass(frozen=True)
class CostEstimate:
    """What one call is expected to cost. `usd` is the paid-plan price."""

    usd: float = 0.0
    credits: float = 0.0
    credit_bucket: str = "default"


@dataclass
class HttpRequest:
    """A plain HTTP request described by a provider module.

    `headers` may hold the API key; they are sent but never hashed, logged or cached.
    `hash_params` identifies the request for the cache (defaults to params + json_body).
    Statuses in `accept_status` other than 200 (e.g. 404 = "no match") are returned
    as {"_http_status": code, "body": <json>} and cached like a normal answer, unless
    `interpret` is given: then `interpret(status, body)` decides. It returns the data
    to use (and cache), or raises ProviderError / RetryableError / ProviderExhausted.
    `cacheable=False` skips the cache entirely (e.g. live credit balance).
    """

    provider: str
    endpoint: str          # logical name, e.g. "company/enrich"; also the cache_days key
    method: str
    url: str
    params: dict[str, Any] | None = None
    json_body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    hash_params: dict[str, Any] | None = None
    accept_status: frozenset[int] = frozenset({200})
    fixture_key: str | None = None
    interpret: Callable[[int, Any], Any] | None = None
    cacheable: bool = True


@dataclass
class CallContext:
    """Everything a guarded call needs: database, settings, run, budget, mode."""

    conn: sqlite3.Connection
    settings: dict[str, Any]
    run_id: str | None = None
    budget_usd: float | None = None          # None -> settings budget.run_default_usd
    mock: bool = False
    fixtures_dir: Path = FIXTURES_DIR
    http: httpx.Client | None = None
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = utc_now
    clock: Callable[[], float] = time.monotonic     # for throttling
    exhausted: set[str] = field(default_factory=set)  # providers out of credits this run
    _last_call: dict[tuple[str, str], float] = field(default_factory=dict)

    def client(self) -> httpx.Client:
        """Return the shared HTTP client, creating it on first use."""
        if self.http is None:
            timeout = float(self.settings.get("http", {}).get("timeout_seconds", 30))
            self.http = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
        return self.http


# ---------- helpers ----------

def require_key(env_var: str) -> str:
    """Return the API key in `env_var`, or raise MissingApiKey naming the variable."""
    value = os.environ.get(env_var, "").strip()
    if not value:
        raise MissingApiKey(
            f"Missing environment variable {env_var}. Add it to your .env file "
            f"(see .env.example for the list of names)."
        )
    return value


def params_hash(provider: str, endpoint: str, params: dict[str, Any] | None) -> str:
    """Fingerprint a request: same provider/endpoint/params in any order -> same hash.

    Keys whose value is None are dropped so optional params don't change the hash.
    """
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    text = json.dumps({"provider": provider, "endpoint": endpoint, "params": clean},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provider_settings(settings: dict[str, Any], provider: str) -> dict[str, Any]:
    """Return settings.providers.<provider>, or {} if not configured."""
    return (settings.get("providers") or {}).get(provider) or {}


def cache_days(settings: dict[str, Any], provider: str, endpoint: str) -> float:
    """Cache expiry for a call: endpoint-specific, then provider default, then global default."""
    days = provider_settings(settings, provider).get("cache_days") or {}
    if endpoint in days:
        return float(days[endpoint])
    if "default" in days:
        return float(days["default"])
    return float(settings.get("cache_default_days", 30))


def min_interval(settings: dict[str, Any], provider: str, endpoint: str) -> float:
    """Seconds to wait between calls to one endpoint (endpoint, then provider default, else 0)."""
    gaps = provider_settings(settings, provider).get("min_interval_seconds") or {}
    return float(gaps.get(endpoint, gaps.get("default", 0.0)))


def _throttle(ctx: CallContext, provider: str, endpoint: str) -> None:
    """Sleep if the previous call to this endpoint was too recent (respects rate limits)."""
    gap = min_interval(ctx.settings, provider, endpoint)
    key = (provider, endpoint)
    last = ctx._last_call.get(key)
    if gap > 0 and last is not None:
        wait = gap - (ctx.clock() - last)
        if wait > 0:
            ctx.sleep(wait)
    ctx._last_call[key] = ctx.clock()


def dollars(settings: dict[str, Any], provider: str, usd: float) -> float:
    """Dollar cost to log: $0 on a free plan, the estimate otherwise (unknown = paid)."""
    plan = provider_settings(settings, provider).get("plan", "paid")
    return 0.0 if plan == "free" else usd


def load_fixture(fixtures_dir: Path, provider: str, endpoint: str,
                 fixture_key: str | None = None) -> Any:
    """Load tests/fixtures/<provider>/<endpoint>[__<key>].json ('/' in endpoint -> '__')."""
    base = endpoint.replace("/", "__")
    folder = fixtures_dir / provider
    candidates = ([folder / f"{base}__{fixture_key}.json"] if fixture_key else []) + \
                 [folder / f"{base}.json"]
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    raise MockFixtureMissing(f"No fixture for {provider} {endpoint} (looked for {candidates[0].name})")


def check_budget(ctx: CallContext, provider: str, endpoint: str, phash: str,
                 est: CostEstimate) -> None:
    """Raise BudgetExceeded (after logging the refusal) if this call would break a limit."""
    budget_cfg = ctx.settings.get("budget") or {}
    run_budget = ctx.budget_usd if ctx.budget_usd is not None else \
        float(budget_cfg.get("run_default_usd", 5.0))
    month_cap = float(budget_cfg.get("monthly_cap_usd", 25.0))
    usd = dollars(ctx.settings, provider, est.usd)
    now = ctx.now()

    reason = None
    run_spent = db.run_spend(ctx.conn, ctx.run_id) if ctx.run_id else 0.0
    month_spent = db.month_spend(ctx.conn, now=now)
    if run_spent + usd > run_budget + 1e-9:
        reason = f"run budget ${run_budget:.2f} would be exceeded (spent ${run_spent:.4f}, call ${usd:.4f})"
    elif month_spent + usd > month_cap + 1e-9:
        reason = f"monthly cap ${month_cap:.2f} would be exceeded (spent ${month_spent:.4f}, call ${usd:.4f})"
    elif est.credits > 0:
        caps = provider_settings(ctx.settings, provider).get("monthly_credit_caps") or {}
        cap = caps.get(est.credit_bucket, caps.get("default"))
        if cap is not None:
            used = db.month_credits(ctx.conn, provider, est.credit_bucket, now=now)
            if used + est.credits > float(cap) + 1e-9:
                reason = (f"{provider} {est.credit_bucket} credits would exceed monthly cap "
                          f"{cap} (used {used:g}, call {est.credits:g})")
    if reason:
        db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                        params_hash=phash, status_code=None, credit_bucket=est.credit_bucket,
                        error=f"refused: {reason}", now=now)
        log.warning("Budget guard refused %s %s: %s", provider, endpoint, reason)
        raise BudgetExceeded(reason)


def _with_retries(ctx: CallContext, send: Callable[[], Any], label: str) -> Any:
    """Run `send`, retrying RetryableError with waits of base, 2*base, 4*base, ..."""
    http_cfg = ctx.settings.get("http") or {}
    retries = int(http_cfg.get("max_retries", 4))
    base = float(http_cfg.get("backoff_base_seconds", 2))
    retrying = Retrying(
        stop=stop_after_attempt(retries + 1),
        wait=wait_exponential(multiplier=base, exp_base=2, max=60),
        retry=retry_if_exception_type(RetryableError),
        sleep=ctx.sleep,
        reraise=True,
        before_sleep=lambda rs: log.info("Retrying %s after: %s (attempt %d)", label,
                                         rs.outcome.exception(), rs.attempt_number),
    )
    return retrying(send)


# ---------- the main entry points ----------

def guarded_call(ctx: CallContext, *, provider: str, endpoint: str,
                 hash_params: dict[str, Any] | None, estimate: CostEstimate,
                 send: Callable[[], Any],
                 actual: Callable[[Any], CostEstimate] | None = None,
                 fixture_key: str | None = None, cacheable: bool = True) -> Any:
    """Run one paid call through mock/cache/budget/retry/log. Returns JSON-able data.

    `send` performs the real call and must raise RetryableError for temporary
    failures and ProviderError for permanent ones (ProviderExhausted = out of
    credits, which blocks further calls to this provider for the run). `actual`,
    if given, computes the real cost from the response (e.g. Claude token usage,
    credits only when found).
    """
    phash = params_hash(provider, endpoint, hash_params)
    label = f"{provider} {endpoint}"

    if ctx.mock:
        data = load_fixture(ctx.fixtures_dir, provider, endpoint, fixture_key)
        db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                        params_hash=phash, status_code=200, credit_bucket=estimate.credit_bucket,
                        error="mock", now=ctx.now())
        return data

    cached = db.cache_get(ctx.conn, provider, endpoint, phash,
                          cache_days(ctx.settings, provider, endpoint),
                          now=ctx.now()) if cacheable else None
    if cached is not None:
        db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                        params_hash=phash, status_code=200, credit_bucket=estimate.credit_bucket,
                        cached=True, now=ctx.now())
        return cached

    if provider in ctx.exhausted:
        reason = f"{provider} reported it is out of credits earlier in this run"
        db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                        params_hash=phash, status_code=None, credit_bucket=estimate.credit_bucket,
                        error=f"refused: {reason}", now=ctx.now())
        raise ProviderExhausted(reason)

    check_budget(ctx, provider, endpoint, phash, estimate)

    def throttled_send() -> Any:
        _throttle(ctx, provider, endpoint)
        return send()

    try:
        data = _with_retries(ctx, throttled_send, label)
    except Exception as exc:  # log every failure, then hand a ProviderError to the stage
        if isinstance(exc, ProviderExhausted):
            ctx.exhausted.add(provider)
        status = getattr(exc, "status_code", None)
        message = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
        db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                        params_hash=phash, status_code=status,
                        credit_bucket=estimate.credit_bucket, error=message[:500], now=ctx.now())
        log.warning("%s failed: %s", label, message[:200])
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError(message, status) from None

    final = actual(data) if actual else estimate
    db.log_api_call(ctx.conn, run_id=ctx.run_id, provider=provider, endpoint=endpoint,
                    params_hash=phash, status_code=200,
                    est_cost_usd=dollars(ctx.settings, provider, final.usd),
                    credits=final.credits, credit_bucket=final.credit_bucket, now=ctx.now())
    if cacheable:
        db.cache_put(ctx.conn, provider, endpoint, phash, data, now=ctx.now())
    return data


def call_json(ctx: CallContext, req: HttpRequest, estimate: CostEstimate,
              actual: Callable[[Any], CostEstimate] | None = None) -> Any:
    """Send a plain HTTP request through `guarded_call` and return the parsed JSON."""
    label = f"{req.provider} {req.endpoint}"

    def send() -> Any:
        try:
            # User-Agent is set on every request, whoever created the client.
            headers = {"User-Agent": USER_AGENT, **req.headers}
            resp = ctx.client().request(req.method, req.url, params=req.params,
                                        json=req.json_body, headers=headers)
        except httpx.TimeoutException:
            raise RetryableError(f"{label}: timeout") from None   # `from None` keeps URLs out
        except httpx.TransportError as exc:
            raise RetryableError(f"{label}: network error ({type(exc).__name__})") from None
        code = resp.status_code
        # A provider-specific interpreter gets first say on statuses it accepts
        # (e.g. Hunter uses 429 for "out of credits", not "slow down").
        if req.interpret is not None and code in req.accept_status:
            try:
                body = resp.json()
            except ValueError:
                body = {"_raw": resp.text[:300]}
            return req.interpret(code, body)
        if code == 429 or code >= 500:
            raise RetryableError(f"{label}: HTTP {code}", code)
        if code not in req.accept_status:
            raise ProviderHTTPError(f"{label}: HTTP {code}: {resp.text[:300]}", code)
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(f"{label}: response was not JSON", code) from None
        if req.interpret is not None:
            return req.interpret(code, body)
        return body if code == 200 else {"_http_status": code, "body": body}

    hash_params = req.hash_params if req.hash_params is not None else \
        {"params": req.params, "json": req.json_body}
    data = guarded_call(ctx, provider=req.provider, endpoint=req.endpoint,
                        hash_params=hash_params, estimate=estimate, send=send,
                        actual=actual, fixture_key=req.fixture_key,
                        cacheable=req.cacheable)
    if ctx.mock and req.interpret is not None:
        # Fixtures are raw API bodies, so run them through the same interpretation.
        status = 400 if isinstance(data, dict) and data.get("error") else 200
        data = req.interpret(status, data)
    return data
