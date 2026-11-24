"""The full pipeline: discover -> qualify -> score -> contacts -> enrich -> clean/export.

Each stage stores its results in SQLite, so a crashed run can be re-run and cached
API answers are reused at no cost. Stages never crash the run on a provider error;
they log it and carry on with what they have.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import db
from src.config import resolve
from src.contacts import find_contacts
from src.discover import discover
from src.enrich import enrich
from src.export import export
from src.models import ICP, Tier
from src.providers.base import CallContext
from src.qualify import qualify
from src.score import inputs_from_records, score_account, tier_at_least

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    run_id: str
    candidates: int = 0
    tiers: dict[str, int] = field(default_factory=dict)
    contacts: int = 0
    emails: dict[str, int] = field(default_factory=dict)
    rows: int = 0
    needs_review: int = 0
    paths: dict[str, Path] = field(default_factory=dict)
    spent_usd: float = 0.0


def load_suppression(conn, settings: dict[str, Any]) -> int:
    """Load the real list (data/suppression.csv) if present, else the empty template."""
    paths = settings["paths"]
    for key in ("suppression_file", "suppression_template"):
        path = resolve(paths[key])
        if path.exists():
            return db.load_suppression_csv(conn, path)
    return 0


def score_all(ctx: CallContext, icp: ICP, scoring: dict[str, Any], settings: dict[str, Any],
              domains: list[str], failures: dict[str, str]) -> dict[str, Tier]:
    """Score every candidate and store the result. Unassessed companies still get a score."""
    tiers: dict[str, Tier] = {}
    for domain in domains:
        inp = inputs_from_records(domain, icp, db.get_company(ctx.conn, domain),
                                  db.get_assessment(ctx.conn, domain),
                                  db.get_signals(ctx.conn, domain))
        s = score_account(inp, icp, scoring, settings["tiers"], now=ctx.now())
        if domain in failures and s.exclusion_reason is None:
            s = s.model_copy(update={"parts": {**s.parts, "note": failures[domain]}})
        db.upsert_score(ctx.conn, s, now=ctx.now())
        tiers[domain] = s.tier
    return tiers


def run_pipeline(ctx: CallContext, icp: ICP, settings: dict[str, Any],
                 scoring: dict[str, Any], target_accounts: int) -> PipelineResult:
    """Run every stage in order and write the CSVs. `ctx.run_id` must be set."""
    assert ctx.run_id, "run_id required"
    result = PipelineResult(run_id=ctx.run_id)

    disc = discover(ctx, icp, settings, scoring, target_accounts)
    result.candidates = len(disc.candidates)

    failures = qualify(ctx, icp, settings, scoring, disc.candidates, disc.names)
    tiers = score_all(ctx, icp, scoring, settings, disc.candidates, failures)
    for t in tiers.values():
        result.tiers[str(t)] = result.tiers.get(str(t), 0) + 1

    # Best accounts first; only buy contacts for tier >= min_tier_for_contacts,
    # and never for more than target_accounts accounts.
    minimum = settings.get("min_tier_for_contacts", "B")
    ranked = sorted((d for d, t in tiers.items() if tier_at_least(t, minimum)),
                    key=lambda d: [x.value for x in Tier].index(tiers[d]))[:target_accounts]
    chosen = find_contacts(ctx, icp, settings, ranked)
    result.contacts = sum(len(v) for v in chosen.values())
    result.emails = dict(enrich(ctx, settings, chosen))

    out_dir = resolve(settings["paths"]["output_dir"])
    cleaned, paths = export(ctx.conn, ctx.run_id, out_dir, disc.candidates, disc.excluded,
                            disc.names)
    result.rows, result.needs_review, result.paths = (len(cleaned.rows),
                                                      len(cleaned.needs_review), paths)
    result.spent_usd = db.run_spend(ctx.conn, ctx.run_id)
    return result
