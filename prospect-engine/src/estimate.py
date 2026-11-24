"""Estimate (spec rule 11): calls, credits and dollars a run could use, before spending.

Pure arithmetic over the ICP + settings, using each provider's own estimate_cost().
Every number is a worst case (e.g. every Prospeo email lookup fails, so Hunter is
tried for everyone), except Claude's "typical" dollars, which assume a normal-length
answer instead of the full max_tokens.
"""

import math
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from src import db
from src.models import ICP
from src.providers import claude, hunter, prospeo, tavily, theirstack
from src.providers.base import CostEstimate, dollars, provider_settings

KEY_VARS = [  # (env var, required?, used for)
    ("ANTHROPIC_API_KEY", True, "Claude: qualification and title classification"),
    ("THEIRSTACK_API_KEY", True, "TheirStack: SDR/BDR job posts (hiring signal)"),
    ("PROSPEO_API_KEY", True, "Prospeo: company filter, people search, emails"),
    ("HUNTER_API_KEY", True, "Hunter: fallback email finder + verifier"),
    ("TAVILY_API_KEY", True, "Tavily: public page text + funding news"),
    ("FIRECRAWL_API_KEY", False, "Firecrawl: not used (D1)"),
    ("GOOGLE_SERVICE_ACCOUNT_FILE", False, "Google Sheets export (step 8, optional)"),
]


@dataclass
class Line:
    """One provider endpoint in the estimate."""

    provider: str
    endpoint: str
    calls: int
    credits: float
    credit_bucket: str
    usd_worst: float
    usd_typical: float
    note: str = ""


@dataclass
class Estimate:
    target_accounts: int
    candidates: int
    lines: list[Line]
    budget_usd: float
    month_spent_usd: float
    month_cap_usd: float
    warnings: list[str]

    @property
    def usd_worst(self) -> float:
        return sum(l.usd_worst for l in self.lines)

    @property
    def usd_typical(self) -> float:
        return sum(l.usd_typical for l in self.lines)


def _line(settings: dict[str, Any], provider: str, endpoint: str, calls: int,
          per_call: CostEstimate, typical: CostEstimate | None = None, note: str = "") -> Line:
    typical = typical or per_call
    return Line(provider, endpoint, calls, per_call.credits * calls, per_call.credit_bucket,
                dollars(settings, provider, per_call.usd) * calls,
                dollars(settings, provider, typical.usd) * calls, note)


def build_lines(icp: ICP, settings: dict[str, Any], target: int) -> tuple[int, list[Line]]:
    """Worst-case calls per provider for a run aiming at `target` accounts."""
    est_cfg = settings.get("estimate") or {}
    multiplier = int((settings.get("discover") or {}).get("candidate_multiplier", 2))
    candidates = target * multiplier
    contacts = target * int((settings.get("contacts") or {}).get("contacts_per_account", 3))

    ts = provider_settings(settings, "theirstack").get("search") or {}
    page_size, ts_pages = int(ts.get("page_size", 25)), int(ts.get("max_pages", 4))
    ps = provider_settings(settings, "prospeo").get("search") or {}
    hiring_companies = ts_pages * page_size          # at most one company per job
    company_pages = min(int(ps.get("max_company_pages", 4)),
                        math.ceil(hiring_companies / 25))
    person_pages = min(int(ps.get("max_person_pages", 10)),
                       math.ceil(target * int(ps.get("max_person_per_company", 5)) / 25))
    n_urls = int((settings.get("qualify") or {}).get("max_pages_per_company", 3))
    funding_mode = (settings.get("qualify") or {}).get("funding_search", "if_missing")

    models = settings.get("models") or {}
    q_model = models.get("qualify", "claude-sonnet-5-5")
    t_model = models.get("title_classifier", "claude-haiku-5-5")
    q_max = int((provider_settings(settings, "claude").get("max_tokens") or {}).get("qualify", 8000))
    t_max = int((provider_settings(settings, "claude").get("max_tokens") or {})
                .get("classify-titles", 4000))
    q_chars = int((settings.get("qualify") or {}).get("max_page_tokens", 6000)) * 4 + \
        int(est_cfg.get("qualify_overhead_chars", 6000))
    t_chars = contacts * 2 * 60 + 2000

    lines = [
        _line(settings, "theirstack", "jobs/search", ts_pages,
              theirstack.estimate_cost("jobs/search", page_size),
              note="1 credit per job returned; stops early once enough companies"),
        _line(settings, "prospeo", "search-company", company_pages,
              prospeo.estimate_cost("search-company"),
              note="1 credit per page with results"),
        _line(settings, "tavily", "extract", candidates,
              tavily.estimate_cost("extract", n_urls),
              note=f"{n_urls} pages per company; failed pages free"),
        _line(settings, "tavily", "search", candidates, tavily.estimate_cost("search"),
              note="funding news; skipped when a dated round is already known"
              if funding_mode == "if_missing" else "funding news, every company"),
        _line(settings, "claude", f"qualify ({q_model})", candidates,
              claude.estimate_cost(settings, q_model, q_chars, q_max),
              claude.estimate_cost(settings, q_model, q_chars,
                                   int(est_cfg.get("qualify_typical_output_tokens", 2000))),
              note=f"worst = {q_max} output tokens each"),
        _line(settings, "prospeo", "search-person", person_pages,
              prospeo.estimate_cost("search-person"), note="1 credit per page with results"),
        _line(settings, "claude", f"classify-titles ({t_model})", 1,
              claude.estimate_cost(settings, t_model, t_chars, t_max),
              claude.estimate_cost(settings, t_model, t_chars,
                                   int(est_cfg.get("classify_typical_output_tokens", 800))),
              note="only titles the rules can't decide"),
        _line(settings, "prospeo", "enrich-person", contacts,
              prospeo.estimate_cost("enrich-person"),
              note="charged only for a verified email"),
        _line(settings, "hunter", "email-finder", contacts, hunter.estimate_cost("email-finder"),
              note="only if Prospeo has no email; charged only if found"),
        _line(settings, "hunter", "email-verifier", contacts,
              hunter.estimate_cost("email-verifier"), note="only for Hunter-found emails"),
    ]
    return candidates, lines


def estimate_run(conn: sqlite3.Connection, icp: ICP, settings: dict[str, Any], target: int,
                 budget_usd: float | None = None, now=None) -> Estimate:
    """Build the estimate and compare it with budgets and monthly credit caps."""
    candidates, lines = build_lines(icp, settings, target)
    budget_cfg = settings.get("budget") or {}
    budget = budget_usd if budget_usd is not None else float(budget_cfg.get("run_default_usd", 5))
    month_cap = float(budget_cfg.get("monthly_cap_usd", 25))
    month_spent = db.month_spend(conn, now=now)
    est = Estimate(target, candidates, lines, budget, month_spent, month_cap, [])

    if est.usd_worst > budget:
        est.warnings.append(
            f"Worst case ${est.usd_worst:.2f} is over the ${budget:.2f} run budget: the budget "
            "guard will stop paid calls when it is reached (later companies stay unassessed).")
    if month_spent + est.usd_worst > month_cap:
        est.warnings.append(f"Worst case would pass the ${month_cap:.2f} monthly cap "
                            f"(${month_spent:.2f} already spent this month).")

    by_bucket: dict[tuple[str, str], float] = {}
    for l in lines:
        if l.credits:
            by_bucket[(l.provider, l.credit_bucket)] = \
                by_bucket.get((l.provider, l.credit_bucket), 0) + l.credits
    for (provider, bucket), credits in sorted(by_bucket.items()):
        caps = provider_settings(settings, provider).get("monthly_credit_caps") or {}
        cap = caps.get(bucket, caps.get("default"))
        if cap is None:
            continue
        used = db.month_credits(conn, provider, bucket, now=now)
        if used + credits > float(cap):
            est.warnings.append(
                f"{provider}: worst case {credits:g} credits + {used:g} used this month is over "
                f"the {cap} monthly cap. The guard stops {provider} calls at the cap.")
    return est


def key_status() -> list[tuple[str, bool, bool, str]]:
    """(name, required, is_set, purpose) for each key. Never returns values."""
    return [(name, req, bool(os.environ.get(name, "").strip()), purpose)
            for name, req, purpose in KEY_VARS]
