"""What a registration must say, and the intake/v2 record an approval turns it into."""

import calendar
import datetime as dt
import re

from .contracts import validate

CLASSIFICATIONS = ("public", "internal", "restricted", "phi")
TIERS = ("low", "moderate", "high")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def add_months(day: dt.date, months: int) -> dt.date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def form_draft(repo, purpose, classification, tier) -> tuple[dict | None, list[str]]:
    repo, purpose = (repo or "").strip(), (purpose or "").replace("\r\n", "\n").strip()
    errors = []
    if not REPO.fullmatch(repo) or {".", ".."} & set(repo.split("/")):
        errors.append("Choose a repository.")
    if not 10 <= len(purpose) <= 4000:
        errors.append("Describe the purpose in 10 to 4000 characters.")
    if classification not in CLASSIFICATIONS:
        errors.append("Choose a data classification.")
    if tier not in TIERS:
        errors.append("Choose a proposed tier.")
    if errors:
        return None, errors
    draft = {"schema": "registration-draft/v1", "repo": repo, "purpose": purpose, "data_classification": classification, "proposed_tier": tier}
    problems = validate("registration-draft.v1", draft)
    return (None, [f"The registration is not valid: {problems[0]}"]) if problems else (draft, [])


def intake_record(request, *, approver_id, tier, classification, review_by, justification, now) -> tuple[dict | None, list[str]]:
    justification, errors, today = (justification or "").strip(), [], now.date()
    if approver_id == request.requested_by:
        errors.append("Only another admin can approve a request: you registered this one.")
    if tier not in TIERS:
        errors.append("Choose a tier.")
    if classification not in CLASSIFICATIONS:
        errors.append("Choose a data classification.")
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
    if errors:
        return None, errors
    doc = {"schema": "intake/v2", "repo": request.repo, "tier": tier, "purpose": request.draft["purpose"], "owner": request.requested_by,
           "data_classification": classification, "registered_by": request.requested_by, "approved_by": approver_id,
           "approved_at": now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "approval_justification": justification,
           "review_by": review.isoformat(), "request": request.id}
    problems = validate("intake.v2", doc)
    return (None, [f"The record is not valid: {problems[0]}"]) if problems else (doc, [])
