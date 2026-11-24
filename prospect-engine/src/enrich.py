"""Enrich (spec 5.6, D10): email waterfall Prospeo enrich-person -> Hunter finder -> no_email,
then verification. Phones are never requested.
"""

import logging
from collections import Counter
from typing import Any

from src import db
from src.contacts import Candidate
from src.models import Contact, EmailStatus, FieldValue
from src.providers import hunter, prospeo
from src.providers.base import BudgetExceeded, CallContext, ProviderError

log = logging.getLogger(__name__)


def enrich(ctx: CallContext, settings: dict[str, Any],
           chosen: dict[str, list[Candidate]]) -> Counter:
    """Find and verify emails for chosen contacts. Returns a count of email statuses."""
    stats: Counter = Counter()
    for domain, cands in chosen.items():
        for cand in cands:
            p = cand.person
            email, status, source = None, EmailStatus.NO_EMAIL, None

            # 1. Prospeo (verified emails only count as found)
            if "prospeo" not in ctx.exhausted:
                try:
                    res = prospeo.enrich_email(ctx, p.person_id, settings)
                    if res.email:
                        email, status, source = res.email, EmailStatus.VALID, "prospeo"
                except (BudgetExceeded, ProviderError) as exc:
                    log.info("Prospeo enrich failed for %s: %s", p.person_id, exc)

            # 2. Hunter finder + verifier
            if email is None and "hunter" not in ctx.exhausted:
                try:
                    found = hunter.find_email(ctx, domain, p.first_name, p.last_name, p.full_name)
                    if found.email:
                        email, source = found.email, "hunter"
                        status = EmailStatus(found.status) if found.status else \
                            EmailStatus(hunter.verify_email(ctx, found.email))
                except (BudgetExceeded, ProviderError) as exc:
                    log.info("Hunter failed for %s at %s: %s", p.full_name, domain, exc)
                    if email and status == EmailStatus.NO_EMAIL:
                        status = EmailStatus.UNKNOWN   # found but verification failed

            contact_id = Contact.make_id(domain, p.full_name)
            stored = db.upsert_contact(ctx.conn, Contact(
                contact_id=contact_id, domain=domain, full_name=p.full_name,
                email=email, email_status=status), now=ctx.now())
            if not stored:   # email already belongs to another contact (duplicate)
                stats["duplicate"] += 1
                continue
            stats[str(status)] += 1
            if email:
                db.add_field_value(ctx.conn, FieldValue(
                    entity_type="contact", entity_id=contact_id, field="email", value=email,
                    source=source, retrieved_at=ctx.now(),
                    confidence=0.95 if status == EmailStatus.VALID else 0.6))
                db.add_field_value(ctx.conn, FieldValue(
                    entity_type="contact", entity_id=contact_id, field="email_status",
                    value=str(status), source=source, retrieved_at=ctx.now(), confidence=0.9))
    log.info("Emails: %s", dict(stats))
    return stats
