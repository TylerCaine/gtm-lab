"""Export (spec 5.8): build rows from the database, clean them, write the CSVs.

Files (all in output/, gitignored):
  <run_id>_prospects.csv     one row per contact, sorted by tier then score
  <run_id>_accounts.csv      one row per account, including tier C and excluded (with reason)
  <run_id>_needs_review.csv  rows flagged by clean.py
  <run_id>_changes.csv       every change clean.py made
"""

import csv
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src import db
from src.clean import CleanResult, clean_rows
from src.score import TIER_ORDER

PROSPECT_COLUMNS = ["tier", "score", "fit_score", "intent_score", "company", "domain",
                    "employee_count", "contact", "title", "persona", "email", "email_status",
                    "top_signal", "hiring_signal_url", "funding_evidence_url", "fit_reasons",
                    "evidence_url", "confidence", "sources", "retrieved_at", "approved"]
ACCOUNT_COLUMNS = ["tier", "score", "fit_score", "intent_score", "company", "domain",
                   "employee_count", "status", "contacts", "top_signal", "fit_reasons",
                   "disqualifiers", "evidence_url"]


def _tier_rank(tier: str | None) -> int:
    return [t.value for t in TIER_ORDER].index(tier) if tier in [t.value for t in TIER_ORDER] \
        else len(TIER_ORDER)


def _account_info(conn: sqlite3.Connection, domain: str) -> dict[str, Any]:
    """Company + score + assessment + signal summary for one domain."""
    co = conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
    sc = conn.execute("SELECT * FROM scores WHERE domain = ?", (domain,)).fetchone()
    a = conn.execute("SELECT * FROM assessments WHERE domain = ?", (domain,)).fetchone()
    sigs = conn.execute("SELECT * FROM signals WHERE domain = ? ORDER BY observed_at DESC",
                        (domain,)).fetchall()
    hiring = [s for s in sigs if s["signal_type"] == "hiring"]
    funding = [s for s in sigs if s["signal_type"] == "funding"]
    techs = sorted({s["detail"] for s in sigs if s["signal_type"] == "tech"})

    parts = []
    if hiring:
        newest = next((s["observed_at"] for s in hiring if s["observed_at"]), None)
        parts.append(f"{len(hiring)} matching job post(s)"
                     + (f", newest {newest[:10]}" if newest else ""))
    if funding:
        parts.append(f"funding: {funding[0]['detail']}")
    if techs:
        parts.append(f"uses {', '.join(techs)}")

    funding_url = None
    if a and a["funding_evidence"]:
        funding_url = json.loads(a["funding_evidence"]).get("url")
    funding_url = funding_url or next((s["source_url"] for s in funding if s["source_url"]), None)
    evidence = json.loads(a["evidence_urls"]) if a else []
    return {
        "tier": sc["tier"] if sc else "excluded",
        "score": sc["total"] if sc else "",
        "fit_score": sc["fit_score"] if sc else "",
        "intent_score": sc["intent_score"] if sc else "",
        "company": (co["name"] if co else None) or domain,
        "domain": domain,
        "employee_count": co["employee_count"] if co else "",
        "top_signal": "; ".join(parts),
        "hiring_signal_url": next((s["source_url"] for s in hiring if s["source_url"]), ""),
        "funding_evidence_url": funding_url or "",
        "fit_reasons": " | ".join(json.loads(a["reasons"])) if a else "",
        "disqualifiers": " | ".join(json.loads(a["disqualifiers"])) if a else "",
        "evidence_url": evidence[0] if evidence else "",
        "confidence": a["confidence"] if a else "",
        "exclusion_reason": sc["exclusion_reason"] if sc else None,
    }


def _provenance(conn: sqlite3.Connection, entities: list[tuple[str, str]]) -> tuple[str, str]:
    """Distinct sources and the latest retrieved_at across the given (type, id) entities."""
    sources: set[str] = set()
    latest = ""
    for etype, eid in entities:
        for r in conn.execute("SELECT source, MAX(retrieved_at) AS at FROM field_values "
                              "WHERE entity_type = ? AND entity_id = ? GROUP BY source",
                              (etype, eid)):
            sources.add(r["source"])
            latest = max(latest, r["at"] or "")
    return "; ".join(sorted(sources)), latest


def build_prospect_rows(conn: sqlite3.Connection, domains: list[str]) -> list[dict[str, Any]]:
    """One row per stored contact at the given domains (tier A/B/C only)."""
    rows = []
    for domain in domains:
        info = _account_info(conn, domain)
        if info["tier"] == "excluded":
            continue
        for c in conn.execute("SELECT * FROM contacts WHERE domain = ? ORDER BY full_name",
                              (domain,)):
            sources, retrieved = _provenance(conn, [("company", domain),
                                                    ("contact", c["contact_id"])])
            rows.append({**{k: info[k] for k in PROSPECT_COLUMNS if k in info},
                         "contact": c["full_name"], "title": c["title"],
                         "persona": c["persona"], "email": c["email"],
                         "email_status": c["email_status"] or "no_email",
                         "sources": sources, "retrieved_at": retrieved, "approved": ""})
    rows.sort(key=lambda r: (_tier_rank(r["tier"]), -float(r["score"] or 0), r["company"],
                             r["contact"] or ""))
    return rows


def build_account_rows(conn: sqlite3.Connection, domains: list[str],
                       excluded: dict[str, str], names: dict[str, str]) -> list[dict[str, Any]]:
    """One row per account seen this run, scored or excluded, with the reason."""
    rows = []
    for domain in domains:
        info = _account_info(conn, domain)
        n = conn.execute("SELECT COUNT(*) FROM contacts WHERE domain = ?", (domain,)).fetchone()[0]
        status = info["exclusion_reason"] or excluded.get(domain) or "scored"
        rows.append({**{k: info.get(k, "") for k in ACCOUNT_COLUMNS}, "status": status,
                     "contacts": n})
    for domain, reason in excluded.items():
        if domain not in domains:
            rows.append({k: "" for k in ACCOUNT_COLUMNS} | {
                "tier": "excluded", "company": names.get(domain, domain), "domain": domain,
                "status": reason, "contacts": 0})
    rows.sort(key=lambda r: (_tier_rank(r["tier"]), -float(r["score"] or 0), r["domain"]))
    return rows


def _write(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in columns})


def export(conn: sqlite3.Connection, run_id: str, out_dir: Path, scored_domains: list[str],
           excluded: dict[str, str], names: dict[str, str]) -> tuple[CleanResult, dict[str, Path]]:
    """Write all four CSVs. Returns the clean result and the file paths."""
    raw = build_prospect_rows(conn, scored_domains)
    cleaned = clean_rows(raw, db.suppressed_values(conn))
    paths = {
        "prospects": out_dir / f"{run_id}_prospects.csv",
        "accounts": out_dir / f"{run_id}_accounts.csv",
        "needs_review": out_dir / f"{run_id}_needs_review.csv",
        "changes": out_dir / f"{run_id}_changes.csv",
    }
    _write(paths["prospects"], cleaned.rows, PROSPECT_COLUMNS)
    _write(paths["accounts"], build_account_rows(conn, scored_domains, excluded, names),
           ACCOUNT_COLUMNS)
    review_cols = PROSPECT_COLUMNS + ["similar_company", "similar_domain", "review_reason"]
    _write(paths["needs_review"], cleaned.needs_review, review_cols)
    _write(paths["changes"], [asdict(c) for c in cleaned.changes],
           ["row", "field", "old", "new", "rule"])
    return cleaned, paths
