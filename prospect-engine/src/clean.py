"""Clean the final contact rows: normalize, validate, dedupe, suppress (spec 5.7).

Input rows are plain dicts (one per contact) using the export column names,
e.g. company, domain, contact, title, email, email_status, phone, score.
Nothing is ever merged automatically: likely duplicate companies are flagged
for human review. Every change is recorded so it can be written to
output/<run_id>_changes.csv.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import phonenumbers
import tldextract
from rapidfuzz import fuzz

from src.models import normalize_domain

REQUIRED_FIELDS = ("company", "domain", "contact", "title")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Better email statuses first; used to pick which duplicate to keep.
EMAIL_STATUS_RANK = {"valid": 0, "catch_all": 1, "unknown": 2, "no_email": 3, None: 4,
                     "invalid": 5}
COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|hq|gmbh)\b\.?")
FUZZY_THRESHOLD = 90


@dataclass
class Change:
    """One change made to one input row (row numbers start at 1)."""

    row: int
    field: str
    old: Any
    new: Any
    rule: str


@dataclass
class CleanResult:
    """Rows to export, rows for human review, and the log of every change."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    needs_review: list[dict[str, Any]] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


# ---------- normalizers ----------

def normalize_name(name: str | None) -> str | None:
    """Tidy a person's name. Mixed-case names (e.g. 'DeAndre McDonald') are left alone;
    all-lowercase or all-uppercase names are title-cased ('jANE' is mixed, so kept)."""
    if not name:
        return None
    name = " ".join(name.split())
    if name.islower() or name.isupper():
        name = name.title()
    return name


def normalize_phone(phone: str | None, region: str = "US") -> str | None:
    """Return the phone in E.164 form (+14155550100), or None if it can't be parsed."""
    if not phone or not phone.strip():
        return None
    try:
        parsed = phonenumbers.parse(phone, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def is_valid_email(email: str | None) -> bool:
    """Basic syntax check: something@something.tld with no spaces."""
    return bool(email) and EMAIL_RE.match(email) is not None


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # Offline: use the suffix list bundled with tldextract, never download it.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def domain_root(domain: str) -> str:
    """The main part of a domain: 'app.acme.co.uk' -> 'acme'. Unknown TLDs use the first label."""
    ext = _extractor()(domain)
    return ext.domain if ext.suffix else domain.split(".")[0]


def company_key(name: str) -> str:
    """Company name without punctuation or legal suffixes, for fuzzy comparison."""
    name = COMPANY_SUFFIXES.sub(" ", name.lower())
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name).split())


# ---------- main entry point ----------

def clean_rows(rows: list[dict[str, Any]], suppressed: set[str]) -> CleanResult:
    """Normalize, validate, suppress and dedupe contact rows. Input rows are not modified."""
    result = CleanResult()
    candidates: list[tuple[int, dict[str, Any]]] = []

    for i, original in enumerate(rows, start=1):
        row = dict(original)
        problems = _normalize_row(i, row, result.changes)

        # Suppression: the email, the email's domain, or the company domain.
        email = row.get("email")
        domain = row.get("domain")
        email_domain = email.split("@", 1)[1] if email and "@" in email else None
        if (email and email in suppressed) or (domain and domain in suppressed) or \
                (email_domain and email_domain in suppressed):
            result.changes.append(Change(i, "row", None, None, "suppressed"))
            continue

        # Invalid emails are not exported (spec 5.6).
        if row.get("email_status") == "invalid":
            result.changes.append(Change(i, "row", None, None, "email_status_invalid"))
            continue

        missing = [f for f in REQUIRED_FIELDS if not row.get(f)]
        if missing:
            problems.append(f"missing required field(s): {', '.join(missing)}")
        if problems:
            result.needs_review.append({**row, "review_reason": "; ".join(problems)})
            continue

        candidates.append((i, row))

    result.rows = _dedupe_emails(candidates, result.changes)
    result.needs_review.extend(_flag_similar_companies(result.rows))
    return result


def _record(changes: list[Change], i: int, field_name: str, old: Any, new: Any,
            rule: str) -> None:
    if old != new:
        changes.append(Change(i, field_name, old, new, rule))


def _normalize_row(i: int, row: dict[str, Any], changes: list[Change]) -> list[str]:
    """Normalize one row in place, recording changes. Returns any problems found."""
    problems: list[str] = []

    for key, value in list(row.items()):
        if isinstance(value, str):
            stripped = " ".join(value.split())
            if stripped != value:   # empty -> None is not a real change
                _record(changes, i, key, value, stripped or None, "trim")
            row[key] = stripped or None

    if row.get("domain"):
        old = row["domain"]
        try:
            row["domain"] = normalize_domain(old)
            _record(changes, i, "domain", old, row["domain"], "normalize_domain")
        except ValueError:
            problems.append(f"invalid domain: {old}")

    for key in ("contact", "first_name", "last_name"):
        if row.get(key):
            old = row[key]
            row[key] = normalize_name(old)
            _record(changes, i, key, old, row[key], "name_case")

    if row.get("email"):
        old = row["email"]
        row["email"] = old.lower()
        _record(changes, i, "email", old, row["email"], "lowercase_email")
        if not is_valid_email(row["email"]):
            _record(changes, i, "email", row["email"], None, "invalid_email_syntax")
            row["email"] = None
            row["email_status"] = "no_email"

    if row.get("phone"):
        old = row["phone"]
        row["phone"] = normalize_phone(old)
        rule = "e164_phone" if row["phone"] else "unparseable_phone_removed"
        _record(changes, i, "phone", old, row["phone"], rule)

    return problems


def _dedupe_emails(candidates: list[tuple[int, dict[str, Any]]],
                   changes: list[Change]) -> list[dict[str, Any]]:
    """Keep one row per email: the best email status, then the highest score."""
    best: dict[str, tuple[int, dict[str, Any]]] = {}
    no_email: list[tuple[int, dict[str, Any]]] = []

    def rank(row: dict[str, Any]) -> tuple[int, float]:
        return (EMAIL_STATUS_RANK.get(row.get("email_status"), 4), -float(row.get("score") or 0))

    for i, row in candidates:
        email = row.get("email")
        if not email:
            no_email.append((i, row))
            continue
        if email not in best:
            best[email] = (i, row)
            continue
        kept_i, kept = best[email]
        if rank(row) < rank(kept):
            best[email] = (i, row)
            changes.append(Change(kept_i, "row", None, None, f"duplicate_email_of_row_{i}"))
        else:
            changes.append(Change(i, "row", None, None, f"duplicate_email_of_row_{kept_i}"))

    kept_rows = sorted(list(best.values()) + no_email, key=lambda t: t[0])
    return [row for _, row in kept_rows]


def _flag_similar_companies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag pairs of different domains whose names or domain roots look alike (>= 90)."""
    companies: dict[str, str] = {}
    for row in rows:
        companies.setdefault(row["domain"], row["company"])
    domains = sorted(companies)
    flagged: list[dict[str, Any]] = []
    for a_idx, a in enumerate(domains):
        for b in domains[a_idx + 1:]:
            name_score = fuzz.token_sort_ratio(company_key(companies[a]), company_key(companies[b]))
            root_score = fuzz.ratio(domain_root(a), domain_root(b))
            if max(name_score, root_score) >= FUZZY_THRESHOLD:
                flagged.append({
                    "company": companies[a], "domain": a,
                    "similar_company": companies[b], "similar_domain": b,
                    "review_reason": (f"possible duplicate company (name {name_score:.0f}, "
                                      f"domain root {root_score:.0f}); not merged"),
                })
    return flagged
