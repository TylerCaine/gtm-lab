"""SQLite storage: schema and small helper functions.

No other module writes SQL. Every write is idempotent:
- companies, contacts, assessments, scores are upserted on their natural key,
  and a missing (None) value never overwrites a value we already have;
- signals and field_values ignore exact repeats, so re-running from the
  cache adds nothing.

All timestamps are stored as UTC ISO-8601 strings, e.g. "2026-11-24T15:30:00+00:00",
so plain string comparison sorts them correctly.
"""

import csv
import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.models import (
    AccountScore,
    Assessment,
    Company,
    Contact,
    FieldValue,
    Signal,
    normalize_domain,
    utc_now,
)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    icp_id       TEXT,
    budget_usd   REAL,
    spent_usd    REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'running',
    mock         INTEGER NOT NULL DEFAULT 0,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    domain         TEXT PRIMARY KEY,
    name           TEXT,
    employee_count INTEGER,
    country        TEXT,
    industry       TEXT,
    linkedin_url   TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id   TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    full_name    TEXT NOT NULL,
    first_name   TEXT,
    last_name    TEXT,
    title        TEXT,
    persona      TEXT,
    seniority    TEXT,
    email        TEXT,
    email_status TEXT,
    phone        TEXT,
    phone_type   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_contacts_email ON contacts(email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_contacts_domain ON contacts(domain);

CREATE TABLE IF NOT EXISTS field_values (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT NOT NULL,
    field        TEXT NOT NULL,
    value        TEXT,               -- JSON-encoded
    source       TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    confidence   REAL NOT NULL,
    UNIQUE (entity_type, entity_id, field, source, value)
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    domain      TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    detail      TEXT NOT NULL,
    observed_at TEXT,
    source      TEXT NOT NULL,
    source_url  TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (domain, signal_type, source, detail, source_url)
);
CREATE INDEX IF NOT EXISTS ix_signals_domain ON signals(domain);

CREATE TABLE IF NOT EXISTS assessments (
    domain           TEXT PRIMARY KEY,
    icp_fit          TEXT NOT NULL,
    business_model   TEXT NOT NULL,
    sells_to         TEXT,
    reasons          TEXT NOT NULL,  -- JSON list
    disqualifiers    TEXT NOT NULL,  -- JSON list
    evidence_urls    TEXT NOT NULL,  -- JSON list
    funding_evidence TEXT,           -- JSON object or NULL
    confidence       REAL NOT NULL,
    model            TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    domain           TEXT PRIMARY KEY,
    fit_score        REAL NOT NULL,
    intent_score     REAL NOT NULL,
    total            REAL NOT NULL,
    tier             TEXT NOT NULL,
    parts            TEXT NOT NULL,  -- JSON object
    exclusion_reason TEXT,
    scored_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    provider      TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    params_hash   TEXT NOT NULL,
    status_code   INTEGER,
    est_cost_usd  REAL NOT NULL DEFAULT 0,
    credits       REAL NOT NULL DEFAULT 0,
    credit_bucket TEXT NOT NULL DEFAULT 'default',
    cached        INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_calls_run ON api_calls(run_id);
CREATE INDEX IF NOT EXISTS ix_api_calls_provider_at ON api_calls(provider, at);

CREATE TABLE IF NOT EXISTS cache (
    provider      TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    params_hash   TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (provider, endpoint, params_hash)
);

CREATE TABLE IF NOT EXISTS suppression (
    value    TEXT PRIMARY KEY,   -- lowercase email or normalized domain
    reason   TEXT,
    added_at TEXT NOT NULL
);
"""


# ---------- basics ----------

def iso(moment: datetime | None) -> str | None:
    """Format a datetime as a UTC ISO string (seconds precision)."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    """Stable JSON encoding (sorted keys) so equal values give equal text."""
    return json.dumps(value, sort_keys=True, default=str)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the database at `path` and make sure the schema exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Safe to call repeatedly."""
    with conn:
        conn.executescript(SCHEMA)


# ---------- runs ----------

def new_run_id(now: datetime | None = None) -> str:
    """Return a sortable, unique run ID like '20261124-153000-a1b2'."""
    now = now or utc_now()
    return f"{now.astimezone(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def create_run(conn: sqlite3.Connection, run_id: str, icp_id: str, budget_usd: float,
               mock: bool = False, notes: str | None = None,
               now: datetime | None = None) -> None:
    """Record the start of a pipeline run."""
    with conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, icp_id, budget_usd, mock, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, iso(now or utc_now()), icp_id, budget_usd, int(mock), notes),
        )


def finish_run(conn: sqlite3.Connection, run_id: str, status: str,
               notes: str | None = None, now: datetime | None = None) -> None:
    """Mark a run finished and store its total spend (summed from api_calls)."""
    spent = run_spend(conn, run_id)
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, spent_usd = ?, "
            "notes = COALESCE(?, notes) WHERE run_id = ?",
            (iso(now or utc_now()), status, spent, notes, run_id),
        )


# ---------- companies and contacts ----------

def upsert_company(conn: sqlite3.Connection, company: Company,
                   now: datetime | None = None) -> None:
    """Insert or update a company. None values never overwrite existing data."""
    with conn:
        conn.execute(
            """
            INSERT INTO companies (domain, name, employee_count, country, industry,
                                   linkedin_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (domain) DO UPDATE SET
                name           = COALESCE(excluded.name, companies.name),
                employee_count = COALESCE(excluded.employee_count, companies.employee_count),
                country        = COALESCE(excluded.country, companies.country),
                industry       = COALESCE(excluded.industry, companies.industry),
                linkedin_url   = COALESCE(excluded.linkedin_url, companies.linkedin_url),
                updated_at     = excluded.updated_at
            """,
            (company.domain, company.name, company.employee_count, company.country,
             company.industry, company.linkedin_url, iso(now or utc_now())),
        )


def get_company(conn: sqlite3.Connection, domain: str) -> Company | None:
    """Return the stored company for `domain`, or None."""
    row = conn.execute(
        "SELECT domain, name, employee_count, country, industry, linkedin_url "
        "FROM companies WHERE domain = ?", (normalize_domain(domain),)
    ).fetchone()
    return Company(**dict(row)) if row else None


def upsert_contact(conn: sqlite3.Connection, contact: Contact,
                   now: datetime | None = None) -> bool:
    """Insert or update a contact keyed on contact_id.

    Returns False (and logs a warning) instead of crashing if the email already
    belongs to a different contact; clean.py dedupes on email before this point.
    """
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO contacts (contact_id, domain, full_name, first_name, last_name,
                                      title, persona, seniority, email, email_status,
                                      phone, phone_type, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (contact_id) DO UPDATE SET
                    full_name    = excluded.full_name,
                    first_name   = COALESCE(excluded.first_name, contacts.first_name),
                    last_name    = COALESCE(excluded.last_name, contacts.last_name),
                    title        = COALESCE(excluded.title, contacts.title),
                    persona      = COALESCE(excluded.persona, contacts.persona),
                    seniority    = COALESCE(excluded.seniority, contacts.seniority),
                    email        = COALESCE(excluded.email, contacts.email),
                    email_status = COALESCE(excluded.email_status, contacts.email_status),
                    phone        = COALESCE(excluded.phone, contacts.phone),
                    phone_type   = COALESCE(excluded.phone_type, contacts.phone_type),
                    updated_at   = excluded.updated_at
                """,
                (contact.contact_id, contact.domain, contact.full_name, contact.first_name,
                 contact.last_name, contact.title, contact.persona, contact.seniority,
                 contact.email, contact.email_status, contact.phone, contact.phone_type,
                 iso(now or utc_now())),
            )
        return True
    except sqlite3.IntegrityError as exc:
        log.warning("Skipped contact %s: %s", contact.contact_id, exc)
        return False


def get_contact(conn: sqlite3.Connection, contact_id: str) -> Contact | None:
    """Return the stored contact, or None."""
    row = conn.execute(
        "SELECT contact_id, domain, full_name, first_name, last_name, title, persona, "
        "seniority, email, email_status, phone, phone_type FROM contacts WHERE contact_id = ?",
        (contact_id,),
    ).fetchone()
    return Contact(**dict(row)) if row else None


# ---------- provenance and signals ----------

def add_field_value(conn: sqlite3.Connection, fv: FieldValue) -> None:
    """Record one provider-supplied value. Exact repeats are ignored."""
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO field_values (entity_type, entity_id, field, value, "
            "source, retrieved_at, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (fv.entity_type, fv.entity_id, fv.field, _json(fv.value), fv.source,
             iso(fv.retrieved_at), fv.confidence),
        )


def add_signal(conn: sqlite3.Connection, signal: Signal,
               now: datetime | None = None) -> None:
    """Record a signal. A repeat updates its confidence instead of adding a row."""
    with conn:
        conn.execute(
            """
            INSERT INTO signals (domain, signal_type, detail, observed_at, source,
                                 source_url, confidence, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (domain, signal_type, source, detail, source_url) DO UPDATE SET
                confidence  = excluded.confidence,
                observed_at = COALESCE(excluded.observed_at, signals.observed_at)
            """,
            (signal.domain, signal.signal_type, signal.detail, iso(signal.observed_at),
             signal.source, signal.source_url or "", signal.confidence,
             iso(now or utc_now())),
        )


def get_signals(conn: sqlite3.Connection, domain: str,
                signal_type: str | None = None) -> list[Signal]:
    """Return all signals for a domain, optionally filtered by type."""
    sql = ("SELECT domain, signal_type, detail, observed_at, source, source_url, confidence "
           "FROM signals WHERE domain = ?")
    params: list[Any] = [normalize_domain(domain)]
    if signal_type:
        sql += " AND signal_type = ?"
        params.append(signal_type)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [Signal(**{**dict(r), "source_url": r["source_url"] or None}) for r in rows]


# ---------- assessments and scores ----------

def upsert_assessment(conn: sqlite3.Connection, a: Assessment) -> None:
    """Store the latest qualification result for a company (one row per domain)."""
    funding = a.funding_evidence.model_dump(mode="json") if a.funding_evidence else None
    with conn:
        conn.execute(
            """
            INSERT INTO assessments (domain, icp_fit, business_model, sells_to, reasons,
                                     disqualifiers, evidence_urls, funding_evidence,
                                     confidence, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (domain) DO UPDATE SET
                icp_fit = excluded.icp_fit, business_model = excluded.business_model,
                sells_to = excluded.sells_to, reasons = excluded.reasons,
                disqualifiers = excluded.disqualifiers, evidence_urls = excluded.evidence_urls,
                funding_evidence = excluded.funding_evidence, confidence = excluded.confidence,
                model = excluded.model, created_at = excluded.created_at
            """,
            (a.domain, a.icp_fit, a.business_model, a.sells_to, _json(a.fit_reasons),
             _json(a.disqualifiers), _json(a.evidence_urls),
             _json(funding) if funding else None, a.confidence, a.model, iso(a.created_at)),
        )


def get_assessment(conn: sqlite3.Connection, domain: str) -> Assessment | None:
    """Return the stored qualification result for a company, or None."""
    row = conn.execute("SELECT * FROM assessments WHERE domain = ?",
                       (normalize_domain(domain),)).fetchone()
    if not row:
        return None
    return Assessment(
        domain=row["domain"], icp_fit=row["icp_fit"], business_model=row["business_model"],
        sells_to=row["sells_to"] or "", fit_reasons=json.loads(row["reasons"]),
        disqualifiers=json.loads(row["disqualifiers"]),
        evidence_urls=json.loads(row["evidence_urls"]),
        funding_evidence=json.loads(row["funding_evidence"]) if row["funding_evidence"] else None,
        confidence=row["confidence"], model=row["model"],
        created_at=datetime.fromisoformat(row["created_at"]))


def upsert_score(conn: sqlite3.Connection, s: AccountScore,
                 now: datetime | None = None) -> None:
    """Store the latest score for a company (one row per domain)."""
    with conn:
        conn.execute(
            """
            INSERT INTO scores (domain, fit_score, intent_score, total, tier, parts,
                                exclusion_reason, scored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (domain) DO UPDATE SET
                fit_score = excluded.fit_score, intent_score = excluded.intent_score,
                total = excluded.total, tier = excluded.tier, parts = excluded.parts,
                exclusion_reason = excluded.exclusion_reason, scored_at = excluded.scored_at
            """,
            (s.domain, s.fit_score, s.intent_score, s.total, s.tier, _json(s.parts),
             s.exclusion_reason, iso(now or utc_now())),
        )


# ---------- API call log and budget totals ----------

def log_api_call(conn: sqlite3.Connection, *, run_id: str | None, provider: str,
                 endpoint: str, params_hash: str, status_code: int | None,
                 est_cost_usd: float = 0.0, credits: float = 0.0,
                 credit_bucket: str = "default", cached: bool = False,
                 error: str | None = None, now: datetime | None = None) -> int:
    """Append one row to api_calls and return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO api_calls (run_id, provider, endpoint, params_hash, status_code, "
            "est_cost_usd, credits, credit_bucket, cached, error, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, provider, endpoint, params_hash, status_code, est_cost_usd, credits,
             credit_bucket, int(cached), error, iso(now or utc_now())),
        )
    return int(cur.lastrowid)


def _month_start(now: datetime | None) -> str:
    now = (now or utc_now()).astimezone(timezone.utc)
    return iso(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def run_spend(conn: sqlite3.Connection, run_id: str) -> float:
    """Dollars spent by one run (cache hits excluded)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(est_cost_usd), 0) FROM api_calls WHERE run_id = ? AND cached = 0",
        (run_id,),
    ).fetchone()
    return float(row[0])


def month_spend(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    """Dollars spent this calendar month (UTC), all runs, cache hits excluded."""
    row = conn.execute(
        "SELECT COALESCE(SUM(est_cost_usd), 0) FROM api_calls WHERE at >= ? AND cached = 0",
        (_month_start(now),),
    ).fetchone()
    return float(row[0])


def month_credits(conn: sqlite3.Connection, provider: str, credit_bucket: str = "default",
                  now: datetime | None = None) -> float:
    """Credits used this calendar month (UTC) for one provider bucket, cache hits excluded."""
    row = conn.execute(
        "SELECT COALESCE(SUM(credits), 0) FROM api_calls "
        "WHERE provider = ? AND credit_bucket = ? AND at >= ? AND cached = 0",
        (provider, credit_bucket, _month_start(now)),
    ).fetchone()
    return float(row[0])


# ---------- cache ----------

def cache_get(conn: sqlite3.Connection, provider: str, endpoint: str, params_hash: str,
              max_age_days: float, now: datetime | None = None) -> Any | None:
    """Return the cached response if it exists and is younger than `max_age_days`."""
    row = conn.execute(
        "SELECT response_json, fetched_at FROM cache "
        "WHERE provider = ? AND endpoint = ? AND params_hash = ?",
        (provider, endpoint, params_hash),
    ).fetchone()
    if row is None:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    if (now or utc_now()) - fetched > timedelta(days=max_age_days):
        return None
    return json.loads(row["response_json"])


def cache_put(conn: sqlite3.Connection, provider: str, endpoint: str, params_hash: str,
              response: Any, now: datetime | None = None) -> None:
    """Save (or replace) a response in the cache."""
    with conn:
        conn.execute(
            "INSERT INTO cache (provider, endpoint, params_hash, response_json, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (provider, endpoint, params_hash) DO UPDATE SET "
            "response_json = excluded.response_json, fetched_at = excluded.fetched_at",
            (provider, endpoint, params_hash, json.dumps(response), iso(now or utc_now())),
        )


# ---------- suppression ----------

def normalize_suppression_value(value: str) -> str:
    """Emails are lowercased; anything else is treated as a domain and normalized."""
    value = value.strip().lower()
    return value if "@" in value else normalize_domain(value)


def add_suppression(conn: sqlite3.Connection, value: str, reason: str | None = None,
                    csv_path: str | Path | None = None, now: datetime | None = None) -> str:
    """Add an email or domain to the suppression table (and the private CSV, if given).

    Returns the normalized value. Adding the same value twice is harmless.
    """
    norm = normalize_suppression_value(value)
    added_at = iso(now or utc_now())
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO suppression (value, reason, added_at) VALUES (?, ?, ?)",
            (norm, reason, added_at),
        )
    if csv_path is not None and cur.rowcount:
        path = Path(csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["value", "reason", "added_at"])
            writer.writerow([norm, reason or "", added_at])
    return norm


def load_suppression_csv(conn: sqlite3.Connection, csv_path: str | Path) -> int:
    """Load a suppression CSV (value,reason,added_at) into the table. Returns rows read."""
    path = Path(csv_path)
    if not path.exists():
        return 0
    count = 0
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("value", "").strip():
                add_suppression(conn, row["value"], row.get("reason") or None)
                count += 1
    return count


def suppressed_values(conn: sqlite3.Connection) -> set[str]:
    """Return every suppressed email and domain, for fast bulk checks."""
    return {r[0] for r in conn.execute("SELECT value FROM suppression")}


def is_suppressed(conn: sqlite3.Connection, email: str | None = None,
                  domain: str | None = None) -> bool:
    """True if the email, the email's domain, or the domain is on the suppression list."""
    values = suppressed_values(conn)
    if email:
        e = email.strip().lower()
        if e in values or normalize_domain(e) in values:
            return True
    return bool(domain) and normalize_domain(domain) in values
