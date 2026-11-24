"""Contacts (spec 5.5, D10): Prospeo person search for qualifying accounts, then title
classification (rules first, Claude Haiku for the rest) and best-N selection per account.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

from src import db
from src.models import ICP, Contact, FieldValue, Seniority, TitleBatch, TitleClassification
from src.providers import claude, prospeo
from src.providers.base import BudgetExceeded, CallContext, ProviderError, provider_settings

log = logging.getLogger(__name__)

# Titles containing these words are never the economic buyer, whatever else they say.
NEGATIVE = re.compile(r"\b(assistant|intern|associate|coordinator|representative|"
                      r"specialist|analyst|recruiter|consultant|advisor)\b|executive assistant|to the",
                      re.IGNORECASE)
SENIORITY_RANK = {Seniority.C_SUITE: 0, Seniority.VP: 1, Seniority.HEAD: 2,
                  Seniority.DIRECTOR: 3, Seniority.MANAGER: 4,
                  Seniority.INDIVIDUAL_CONTRIBUTOR: 5, Seniority.UNKNOWN: 6, None: 6}
GUESS_SENIORITY = [
    (re.compile(r"\bchief\b|\bc[a-z]o\b", re.I), Seniority.C_SUITE),
    (re.compile(r"\bvp\b|vice president", re.I), Seniority.VP),
    (re.compile(r"\bhead of\b", re.I), Seniority.HEAD),
    (re.compile(r"\bdirector\b", re.I), Seniority.DIRECTOR),
    (re.compile(r"\bmanager\b", re.I), Seniority.MANAGER),
]

HAIKU_SYSTEM = """You classify job titles against buyer personas for B2B sales prospecting.
For each title, return: the title exactly as given; persona = the matching persona name or null;
seniority; department; is_match = true only if the person plausibly holds that persona's role
(senior sales/revenue leadership); confidence 0 to 1. Return one item per title."""


@dataclass
class Candidate:
    person: prospeo.ProspeoPerson
    classification: TitleClassification


def _guess_seniority(title: str) -> Seniority:
    for pattern, level in GUESS_SENIORITY:
        if pattern.search(title):
            return level
    return Seniority.UNKNOWN


def classify_by_rules(title: str, icp: ICP) -> TitleClassification | None:
    """Exact/word-boundary matches against persona titles. None = rules can't decide."""
    if NEGATIVE.search(title):
        return TitleClassification(title=title, persona=None, seniority=_guess_seniority(title),
                                   department="unknown", is_match=False, confidence=0.9)
    low = title.lower()
    for persona in sorted(icp.personas, key=lambda p: p.priority):
        for t in persona.titles:
            if re.search(rf"\b{re.escape(t.lower())}\b", low):
                return TitleClassification(title=title, persona=persona.name,
                                           seniority=_guess_seniority(title),
                                           department="sales", is_match=True, confidence=0.9)
    return None


def classify_titles(ctx: CallContext, titles: list[str], icp: ICP,
                    settings: dict[str, Any]) -> dict[str, TitleClassification]:
    """Rules first; leftovers go to Claude Haiku in one batched call."""
    out: dict[str, TitleClassification] = {}
    leftovers: list[str] = []
    for t in sorted(set(titles)):
        rule = classify_by_rules(t, icp)
        if rule:
            out[t] = rule
        else:
            leftovers.append(t)
    if not leftovers:
        return out
    personas = "\n".join(f"- {p.name} (priority {p.priority}): {', '.join(p.titles)}"
                         for p in icp.personas)
    user = f"PERSONAS:\n{personas}\n\nTITLES:\n" + "\n".join(f"- {t}" for t in leftovers)
    model = (settings.get("models") or {}).get("title_classifier", "claude-haiku-5-5")
    try:
        batch = claude.structured(ctx, task="classify-titles", model=model, system=HAIKU_SYSTEM,
                                  user=user, schema=TitleBatch)
        by_title = {i.title: i for i in batch.items}
        for t in leftovers:
            if t in by_title:
                out[t] = by_title[t]
    except (BudgetExceeded, ProviderError) as exc:
        log.warning("Title classification by Claude failed: %s (titles left unmatched)", exc)
    return out


def find_contacts(ctx: CallContext, icp: ICP, settings: dict[str, Any],
                  domains: list[str]) -> dict[str, list[Candidate]]:
    """Search, classify and keep the best `contacts_per_account` people per domain."""
    if not domains:
        return {}
    cfg = provider_settings(settings, "prospeo").get("search") or {}
    keep = int((settings.get("contacts") or {}).get("contacts_per_account", 3))
    wanted = set(domains)
    people: list[prospeo.ProspeoPerson] = []
    for start in range(0, len(domains), prospeo.MAX_WEBSITES_PER_SEARCH):
        chunk = domains[start:start + prospeo.MAX_WEBSITES_PER_SEARCH]
        for page in range(1, int(cfg.get("max_person_pages", 10)) + 1):
            try:
                data = prospeo.search_people(ctx, chunk, icp, settings, page)
            except (BudgetExceeded, ProviderError) as exc:
                log.warning("Prospeo person search stopped: %s", exc)
                break
            for r in data.get("results") or []:
                p = prospeo.parse_person(r)
                if p and p.domain in wanted and (p.country_code or "US").upper() == "US":
                    people.append(p)
            if page >= int((data.get("pagination") or {}).get("total_page") or 1):
                break

    titles = [p.title for p in people if p.title]
    classes = classify_titles(ctx, titles, icp, settings)
    priority = {p.name: p.priority for p in icp.personas}

    chosen: dict[str, list[Candidate]] = {}
    for p in people:
        c = classes.get(p.title or "")
        if not c or not c.is_match:
            continue
        if p.seniority and c.seniority == Seniority.UNKNOWN:
            c = c.model_copy(update={"seniority": p.seniority})
        chosen.setdefault(p.domain, []).append(Candidate(p, c))
    for domain, cands in chosen.items():
        cands.sort(key=lambda x: (priority.get(x.classification.persona or "", 99),
                                  SENIORITY_RANK.get(x.classification.seniority, 6),
                                  -x.classification.confidence, x.person.full_name))
        unique: dict[str, Candidate] = {}
        for cand in cands:
            unique.setdefault(cand.person.person_id, cand)
        chosen[domain] = list(unique.values())[:keep]
        for cand in chosen[domain]:
            _store(ctx, cand)
    log.info("Contacts: %d people found, %d kept across %d accounts", len(people),
             sum(len(v) for v in chosen.values()), len(chosen))
    return chosen


def _store(ctx: CallContext, cand: Candidate) -> None:
    p, c = cand.person, cand.classification
    contact = Contact(contact_id=Contact.make_id(p.domain, p.full_name), domain=p.domain,
                      full_name=p.full_name, first_name=p.first_name, last_name=p.last_name,
                      title=p.title, persona=c.persona, seniority=c.seniority)
    db.upsert_contact(ctx.conn, contact, now=ctx.now())
    for field_name, value, source, conf in [
        ("full_name", p.full_name, "prospeo", 0.9), ("title", p.title, "prospeo", 0.9),
        ("prospeo_person_id", p.person_id, "prospeo", 1.0),
        ("prospeo_seniority", p.prospeo_seniority, "prospeo", 0.8),
        ("persona", c.persona, "classifier", c.confidence),
    ]:
        if value is not None:
            db.add_field_value(ctx.conn, FieldValue(entity_type="contact",
                                                    entity_id=contact.contact_id,
                                                    field=field_name, value=value, source=source,
                                                    retrieved_at=ctx.now(), confidence=conf))
