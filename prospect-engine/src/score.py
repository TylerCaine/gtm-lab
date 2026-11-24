"""Score accounts: fit (0-100), intent (0-100), total, tier (spec 5.4).

All weights come from config/scoring.yaml and tier cutoffs from settings.yaml.
Every number that goes into a score is written to `parts`, so any score can be
explained line by line.

Rules:
- Fit: a component whose data is unknown is left out and the remaining points
  are rescaled to 100, so missing data never counts against a company (D3).
- Intent: no signal = 0 points for that signal (absence of evidence lowers intent).
- Each intent signal decays by age: factor = 0.5 ** (age_days / half_life_days).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.models import (
    ICP,
    AccountScore,
    Assessment,
    BusinessModel,
    Company,
    IcpFit,
    Signal,
    SignalType,
    Tier,
    utc_now,
)

TIER_ORDER = [Tier.A, Tier.B, Tier.C, Tier.EXCLUDED]


@dataclass
class ScoreInput:
    """The facts about one company that scoring needs."""

    domain: str
    employee_count: int | None = None
    business_model: BusinessModel | None = None
    icp_fit: IcpFit | None = None
    tech_matched: bool | None = None          # True = match found; None = unknown (never "no")
    hiring_dates: list[date | None] = field(default_factory=list)  # one entry per matching job
    funding_date: date | None = None
    funding_confidence: float | None = None


def decay(age_days: float, half_life_days: float) -> float:
    """Halve a signal's value every `half_life_days`. Future dates count as age 0."""
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def inputs_from_records(domain: str, icp: ICP, company: Company | None,
                        assessment: Assessment | None, signals: list[Signal]) -> ScoreInput:
    """Build a ScoreInput from what is stored in the database for one company."""
    techs = {t.lower() for t in icp.soft_signals.technologies}
    tech_signals = [s for s in signals if s.signal_type == SignalType.TECH]
    hiring = [s for s in signals if s.signal_type == SignalType.HIRING]
    funding_signals = [s for s in signals if s.signal_type == SignalType.FUNDING]

    funding_date = None
    funding_conf = None
    if assessment and assessment.funding_evidence and assessment.funding_evidence.date:
        funding_date = assessment.funding_evidence.date
        funding_conf = max([s.confidence for s in funding_signals], default=assessment.confidence)
    elif funding_signals:
        # Structured funding data (Prospeo/TheirStack) recorded during discovery.
        dated = [s for s in funding_signals if s.observed_at]
        if dated:
            newest = max(dated, key=lambda s: s.observed_at)
            funding_date = _as_date(newest.observed_at)
            funding_conf = newest.confidence

    return ScoreInput(
        domain=domain,
        employee_count=company.employee_count if company else None,
        business_model=assessment.business_model if assessment else None,
        icp_fit=assessment.icp_fit if assessment else None,
        tech_matched=True if any(s.detail.lower() in techs for s in tech_signals) else None,
        hiring_dates=[_as_date(s.observed_at) for s in hiring],
        funding_date=funding_date,
        funding_confidence=funding_conf,
    )


def score_account(inp: ScoreInput, icp: ICP, scoring: dict[str, Any],
                  tiers: dict[str, float], now: datetime | None = None) -> AccountScore:
    """Compute fit, intent, total and tier for one company, with a full explanation."""
    today = (now or utc_now()).date()
    fit, fit_parts = _fit_score(inp, icp, scoring["fit"])
    intent, intent_parts = _intent_score(inp, scoring["intent"], today)

    weights = scoring["total"]
    total = weights["fit_weight"] * fit + weights["intent_weight"] * intent

    tier, reason = _tier(total, tiers)
    if inp.icp_fit == IcpFit.NO and scoring["fit"]["llm_icp_fit"].get("exclude_on_no", True):
        tier, reason = Tier.EXCLUDED, "Claude judged ICP fit = no"

    parts = {
        "fit": fit_parts,
        "intent": intent_parts,
        "total": f"{weights['fit_weight']} x fit {fit:.1f} + "
                 f"{weights['intent_weight']} x intent {intent:.1f} = {total:.1f}",
    }
    return AccountScore(domain=inp.domain, fit_score=round(fit, 1),
                        intent_score=round(intent, 1), total=round(total, 1),
                        tier=tier, parts=parts, exclusion_reason=reason)


def _fit_score(inp: ScoreInput, icp: ICP, cfg: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Return (fit 0-100, explanation). Unknown components are left out and rescaled."""
    parts: dict[str, Any] = {}
    earned = 0.0
    possible = 0.0

    def add(name: str, value: float | None, note: str) -> None:
        nonlocal earned, possible
        points = float(cfg[name]["points"])
        if value is None:
            parts[name] = f"unknown ({note}); left out"
            return
        earned += points * value
        possible += points
        parts[name] = f"{note}: {value:g} x {points:g} pts = {points * value:.1f}"

    # Employee range
    er = cfg["employee_range"]
    lo, hi = icp.hard_filters.employee_min, icp.hard_filters.employee_max
    if inp.employee_count is None:
        add("employee_range", None, "no employee count")
    elif lo <= inp.employee_count <= hi:
        add("employee_range", er["in_range"], f"{inp.employee_count} in {lo}-{hi}")
    elif lo * (1 - er["near_range_pct"]) <= inp.employee_count <= hi * (1 + er["near_range_pct"]):
        add("employee_range", er["near_range"], f"{inp.employee_count} near {lo}-{hi}")
    else:
        add("employee_range", er["out_of_range"], f"{inp.employee_count} outside {lo}-{hi}")

    # Business model
    if inp.business_model in (None, BusinessModel.UNKNOWN):
        add("business_model_match", None, "business model not determined")
    else:
        match = inp.business_model == icp.hard_filters.business_model
        add("business_model_match", 1.0 if match else 0.0,
            f"{inp.business_model} {'matches' if match else 'does not match'} "
            f"{icp.hard_filters.business_model}")

    # Claude's ICP judgment
    if inp.icp_fit is None:
        add("llm_icp_fit", None, "no assessment")
    else:
        add("llm_icp_fit", float(cfg["llm_icp_fit"]["values"][str(inp.icp_fit)]),
            f"icp_fit {inp.icp_fit}")

    # Technology (D3: a match is medium-confidence evidence; no match = unknown)
    if not icp.soft_signals.technologies:
        parts["tech_match"] = "not part of this ICP; left out"
    elif inp.tech_matched:
        add("tech_match", float(cfg["tech_match"]["values"]["match"]),
            f"{', '.join(icp.soft_signals.technologies)} seen in job posts")
    else:
        add("tech_match", None, "no tech match in job posts")

    fit = 100.0 * earned / possible if possible else 0.0
    parts["result"] = (f"{earned:.1f} / {possible:.1f} known pts -> {fit:.1f}" if possible
                       else "no known fit data -> 0")
    return fit, parts


def _intent_score(inp: ScoreInput, cfg: dict[str, Any],
                  today: date) -> tuple[float, dict[str, Any]]:
    """Return (intent 0-100, explanation)."""
    parts: dict[str, Any] = {}
    total_points = float(cfg["hiring"]["points"]) + float(cfg["funding"]["points"])
    earned = 0.0

    # Hiring: value from the number of matching jobs, decayed by the newest job's age.
    h = cfg["hiring"]
    n = len(inp.hiring_dates)
    if n == 0:
        parts["hiring"] = "no matching job posts: 0"
    else:
        table = {int(k): float(v) for k, v in h["jobs_to_value"].items()}
        value = table[min(n, max(table))] if n >= min(table) else 0.0
        dates = [d for d in inp.hiring_dates if d is not None]
        if dates:
            age = (today - max(dates)).days
            factor = decay(age, h["half_life_days"])
            age_note = f"newest {age}d old"
        else:
            factor, age_note = 1.0, "dates unknown, no decay"
        pts = value * factor * float(h["points"])
        earned += pts
        parts["hiring"] = (f"{n} job(s) -> {value:g} x decay {factor:.2f} ({age_note}) "
                           f"x {h['points']} pts = {pts:.1f}")

    # Funding: decayed by age, optionally multiplied by confidence.
    f = cfg["funding"]
    if inp.funding_date is None:
        parts["funding"] = "no funding evidence: 0"
    else:
        age = (today - inp.funding_date).days
        factor = decay(age, f["half_life_days"])
        conf = inp.funding_confidence if (f.get("multiply_by_confidence") and
                                          inp.funding_confidence is not None) else 1.0
        pts = factor * conf * float(f["points"])
        earned += pts
        parts["funding"] = (f"funding {age}d ago: decay {factor:.2f} x confidence {conf:.2f} "
                            f"x {f['points']} pts = {pts:.1f} (least reliable signal)")

    intent = 100.0 * earned / total_points if total_points else 0.0
    parts["result"] = f"{earned:.1f} / {total_points:.0f} pts -> {intent:.1f}"
    return intent, parts


def _tier(total: float, tiers: dict[str, float]) -> tuple[Tier, str | None]:
    """Map a total score to a tier using the cutoffs from settings.yaml."""
    for name in ("A", "B", "C"):
        if total >= float(tiers[name]):
            return Tier(name), None
    return Tier.EXCLUDED, f"total {total:.1f} below tier C cutoff {tiers['C']}"


def tier_at_least(tier: Tier | str, minimum: Tier | str) -> bool:
    """True if `tier` is as good as or better than `minimum` (A > B > C > excluded)."""
    return TIER_ORDER.index(Tier(tier)) <= TIER_ORDER.index(Tier(minimum))
