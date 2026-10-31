"""What a registration must say, and the intake/v2 record an approval turns it into."""

import calendar
import datetime as dt
import re

from .classification import RETIRED, retired_message
from .contracts import validate
from .floor import CLASSIFICATIONS, TIERS, below_floor

NO_EVIDENCE = "The platform hasn't computed this repo's evidence yet: approving waits for it."
STALE_EVIDENCE = ("The platform's evidence for this request names a retired classification level: send it back, and the platform "
                  "computes it again when it is submitted again.")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def add_months(day: dt.date, months: int) -> dt.date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def classification_error(value) -> str | None:
    """Why a posted classification can't be used, or None. A retired level names its replacement (spec 2026-10-30 C4)."""
    if value in RETIRED:
        return retired_message(value) + "."
    return None if value in CLASSIFICATIONS else "Choose a data classification."


def form_draft(repo, purpose, classification) -> tuple[dict | None, list[str]]:
    """A registration-draft/v1 from the register or resubmit form. There is no tier: the platform computes a floor and an admin
    decides (spec 2026-10-30 T1), so a posted proposed_tier is never read."""
    repo, purpose = (repo or "").strip(), (purpose or "").replace("\r\n", "\n").strip()
    errors = []
    if not REPO.fullmatch(repo) or {".", ".."} & set(repo.split("/")):
        errors.append("Choose a repository.")
    if not 10 <= len(purpose) <= 4000:
        errors.append("Describe the purpose in 10 to 4000 characters.")
    problem = classification_error(classification)
    if problem:
        errors.append(problem)
    if errors:
        return None, errors
    draft = {"schema": "registration-draft/v1", "repo": repo, "purpose": purpose, "data_classification": classification}
    problems = validate("registration-draft.v1", draft)
    return (None, [f"The registration is not valid: {problems[0]}"]) if problems else (draft, [])


def intake_record(request, *, approver_id, tier, classification, review_by, justification, now, evidence,
                  override_reason="") -> tuple[dict | None, list[str]]:
    """The intake/v2 record an approval turns a registration into. `evidence` is the stored Evidence of the request's current
    submission (or None while the collector hasn't computed it). The record carries its commit and floor; a tier or classification
    below the floor needs the approver's written reason, which goes into the record. A reason given when nothing is below is ignored."""
    justification, errors, today = (justification or "").strip(), [], now.date()
    override_reason = (override_reason or "").strip()
    if approver_id == request.requested_by:
        errors.append("Only another admin can approve a request: you registered this one.")
    if tier not in TIERS:
        errors.append("Choose a tier.")
    problem = classification_error(classification)
    if problem:
        errors.append(problem)
    try:
        review = dt.date.fromisoformat(review_by or "")
    except ValueError:
        review = None
        errors.append("The review date must be a date.")
    if review is not None and review <= today:
        errors.append("The review date must be after today.")
    if review is not None and review > add_months(today, 36):
        errors.append("The review date must be within 3 years.")
    if re.search(r"[\r\n]", justification):
        errors.append("Write the justification on one line.")
    elif not 10 <= len(justification) <= 1000:
        errors.append("Give a justification of 10 to 1000 characters.")
    stale = evidence is not None and evidence.floor_classification not in CLASSIFICATIONS  # computed before builder 0.5.0
    if stale:
        errors.append(STALE_EVIDENCE)
    below = []
    if evidence is not None and not stale and tier in TIERS and classification in CLASSIFICATIONS:
        below = below_floor(tier, classification, evidence.floor_tier, evidence.floor_classification)
        if below and re.search(r"[\r\n]", override_reason):
            errors.append("Write the reason for approving below the floor on one line.")
        elif below and not 30 <= len(override_reason) <= 1000:
            errors.append(f"The {' and '.join(below)} is below the platform's floor ({evidence.floor_tier}, {evidence.floor_classification}): "
                          "give a reason of 30 to 1000 characters to approve it anyway.")
    if errors:
        return None, errors
    if evidence is None:
        return None, [NO_EVIDENCE]
    doc = {"schema": "intake/v2", "repo": request.repo, "tier": tier, "purpose": request.draft["purpose"], "owner": request.requested_by,
           "data_classification": classification, "registered_by": request.requested_by, "approved_by": approver_id,
           "approved_at": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "approval_justification": justification,
           "review_by": review.isoformat(), "request": request.id,
           "evidence_commit": evidence.commit, "floor_tier": evidence.floor_tier, "floor_classification": evidence.floor_classification}
    mark = (evidence.record or {}).get("scorecard")
    if mark in ("per-app", "central"):  # a change keeps the record's scorecard mode: without it a central app goes unreviewed
        doc["scorecard"] = mark
    if below:
        doc["floor_override"] = {"by": approver_id, "at": doc["approved_at"], "reason": override_reason}
    problems = validate("intake.v2", doc)
    return (None, [f"The record is not valid: {problems[0]}"]) if problems else (doc, [])
