import datetime as dt

import pytest
from portal_fakes import T0

from portal.contracts import validate
from portal.registration import NO_EVIDENCE, add_months, form_draft, intake_record
from portal.store.base import Evidence, Request

OWNER, ADMIN, RID = "p_" + "a" * 32, "p_" + "b" * 32, "r_" + "c" * 32


def test_a_complete_form_is_a_valid_draft():
    draft, errors = form_draft("acme/notes", "  Stores short notes.\r\nNo email.  ", "public")
    assert errors == [] and draft["purpose"] == "Stores short notes.\nNo email." and validate("registration-draft.v1", draft) == []


@pytest.mark.parametrize("args, needle", [
    (("", "Stores short notes.", "public"), "Choose a repository"),
    (("acme/notes", "short", "public"), "10 to 4000"),
    (("acme/notes", "x" * 4001, "public"), "10 to 4000"),
    (("acme/notes", "Stores short notes.", "secret"), "data classification"),
    (("acme/../x", "Stores short notes.", "public"), "Choose a repository"),
])
def test_an_incomplete_form_says_what_is_missing(args, needle):
    draft, errors = form_draft(*args)
    assert draft is None and any(needle in e for e in errors)


def request():
    return Request(RID, "registration", "acme/notes", "pending", {"schema": "registration-draft/v1", "repo": "acme/notes",
                   "purpose": "Stores short team notes.", "data_classification": "public"}, OWNER, T0, T0)


EV = Evidence(RID, 1, "e" * 40, {}, "moderate", "public", "f" * 40, T0)


def args(**kw):
    return {**dict(approver_id=ADMIN, tier="moderate", classification="public", review_by="2027-10-23",
                   justification="Checked the purpose.", now=T0, evidence=EV, override_reason=""), **kw}


def test_the_approved_record_is_intake_v2_with_approver_time_and_reason():
    doc, errors = intake_record(request(), **args(classification="confidential", justification="Raised the tier: it stores clinic names."))
    assert errors == [] and validate("intake.v2", doc) == []
    assert (doc["owner"], doc["registered_by"], doc["approved_by"], doc["approved_at"], doc["request"], doc["tier"]) == (
        OWNER, OWNER, ADMIN, "2026-10-23T12:00:00Z", RID, "moderate")


@pytest.mark.parametrize("kw, needle", [
    ({"review_by": "2026-10-23"}, "after today"), ({"review_by": "2030-01-01"}, "within 3 years"), ({"review_by": "soon"}, "a date"),
    ({"justification": "ok"}, "10 to 1000"), ({"justification": "two\nlines here ok"}, "one line"), ({"tier": "huge"}, "tier"),
])
def test_bad_approval_input_is_refused(kw, needle):
    doc, errors = intake_record(request(), **args(**kw))
    assert doc is None and any(needle in e for e in errors)


def test_the_approver_can_not_be_the_requester():
    doc, errors = intake_record(request(), **args(approver_id=OWNER))
    assert doc is None and any("another admin" in e for e in errors)


def test_add_months_clamps_to_the_month_s_end():
    assert add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28) and add_months(dt.date(2026, 10, 23), 12) == dt.date(2027, 10, 23)


def test_the_record_carries_the_evidence_commit_and_floor():
    doc, errors = intake_record(request(), **args())
    assert errors == [] and (doc["evidence_commit"], doc["floor_tier"], doc["floor_classification"]) == ("e" * 40, "moderate", "public")
    assert "floor_override" not in doc and validate("intake.v2", doc) == []


def test_below_the_floor_needs_a_30_character_override_that_goes_into_the_record():
    _, errors = intake_record(request(), **args(tier="low"))
    assert any("below the platform's floor" in e for e in errors)
    _, errors = intake_record(request(), **args(tier="low", override_reason="x" * 29))
    assert any("30 to 1000" in e for e in errors)
    doc, errors = intake_record(request(), **args(tier="low", override_reason="The spec over-declares egress; checked it."))
    assert errors == [] and doc["floor_override"] == {"by": ADMIN, "at": "2026-10-23T12:00:00Z", "reason": "The spec over-declares egress; checked it."}
    assert validate("intake.v2", doc) == []


def test_an_override_reason_at_or_above_the_floor_is_not_recorded():
    doc, _ = intake_record(request(), **args(tier="high", override_reason="Not needed, but typed anyway by the admin."))
    assert "floor_override" not in doc


def test_without_evidence_input_errors_come_first_then_the_wait():
    _, errors = intake_record(request(), **args(evidence=None, justification="ok"))
    assert NO_EVIDENCE not in errors and errors
    _, errors = intake_record(request(), **args(evidence=None))
    assert errors == [NO_EVIDENCE]
