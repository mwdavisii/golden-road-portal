import datetime as dt

import pytest
from portal_fakes import T0

from portal.contracts import validate
from portal.registration import add_months, form_draft, intake_record
from portal.store.base import Request

OWNER, ADMIN, RID = "p_" + "a" * 32, "p_" + "b" * 32, "r_" + "c" * 32


def test_a_complete_form_is_a_valid_draft():
    draft, errors = form_draft("acme/notes", "  Stores short notes.\r\nNo email.  ", "internal", "low")
    assert errors == [] and draft["purpose"] == "Stores short notes.\nNo email." and validate("registration-draft.v1", draft) == []


@pytest.mark.parametrize("args, needle", [
    (("", "Stores short notes.", "internal", "low"), "Choose a repository"),
    (("acme/notes", "short", "internal", "low"), "10 to 4000"),
    (("acme/notes", "x" * 4001, "internal", "low"), "10 to 4000"),
    (("acme/notes", "Stores short notes.", "secret", "low"), "data classification"),
    (("acme/notes", "Stores short notes.", "internal", "none"), "tier"),
    (("acme/../x", "Stores short notes.", "internal", "low"), "Choose a repository"),
])
def test_an_incomplete_form_says_what_is_missing(args, needle):
    draft, errors = form_draft(*args)
    assert draft is None and any(needle in e for e in errors)


def request():
    return Request(RID, "registration", "acme/notes", "pending", {"schema": "registration-draft/v1", "repo": "acme/notes",
                   "purpose": "Stores short team notes.", "data_classification": "internal", "proposed_tier": "low"}, OWNER, T0, T0)


def test_the_approved_record_is_intake_v2_with_approver_time_and_reason():
    doc, errors = intake_record(request(), approver_id=ADMIN, tier="moderate", classification="restricted", review_by="2027-10-23",
                                justification="Raised the tier: it stores clinic names.", now=T0)
    assert errors == [] and validate("intake.v2", doc) == []
    assert (doc["owner"], doc["registered_by"], doc["approved_by"], doc["approved_at"], doc["request"], doc["tier"]) == (
        OWNER, OWNER, ADMIN, "2026-10-23T12:00:00Z", RID, "moderate")


@pytest.mark.parametrize("kw, needle", [
    ({"review_by": "2026-10-23"}, "after today"), ({"review_by": "2030-01-01"}, "within 3 years"), ({"review_by": "soon"}, "a date"),
    ({"justification": "ok"}, "10 to 1000"), ({"justification": "two\nlines here ok"}, "one line"), ({"tier": "huge"}, "tier"),
])
def test_bad_approval_input_is_refused(kw, needle):
    args = dict(approver_id=ADMIN, tier="moderate", classification="internal", review_by="2027-10-23", justification="Checked the purpose.", now=T0)
    doc, errors = intake_record(request(), **{**args, **kw})
    assert doc is None and any(needle in e for e in errors)


def test_the_approver_can_not_be_the_requester():
    doc, errors = intake_record(request(), approver_id=OWNER, tier="low", classification="internal", review_by="2027-10-23",
                                justification="Checked the purpose.", now=T0)
    assert doc is None and any("another admin" in e for e in errors)


def test_add_months_clamps_to_the_month_s_end():
    assert add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28) and add_months(dt.date(2026, 10, 23), 12) == dt.date(2027, 10, 23)
