"""The re-review list's rules (spec 2026-10-31 R3, R5, R8): no I/O, so every edge is a line here."""

import dataclasses as dc
import datetime as dt

import pytest

from portal.rereview import (DEFAULT_SIGNALS, REASON_WORDS, BadSetting, auto_close_note, date_reason, is_stale, matching, parse_days,
                             parse_signals)
from portal.store.base import IntakeObserved, RereviewItem

TODAY = dt.date(2026, 10, 31)
NOW = dt.datetime(2026, 10, 31, 12, tzinfo=dt.UTC)


@pytest.mark.parametrize("review_by, days, reason", [
    (dt.date(2026, 10, 30), 30, "review_overdue"), (dt.date(2026, 10, 31), 30, "review_due_soon"), (dt.date(2026, 11, 30), 30, "review_due_soon"),
    (dt.date(2026, 12, 1), 30, None), (dt.date(2026, 11, 1), 0, None), (dt.date(2026, 10, 1), 0, "review_overdue"), (None, 30, None)])
def test_a_review_date_gives_at_most_one_reason(review_by, days, reason):
    assert date_reason(review_by, TODAY, days) == reason


@pytest.mark.parametrize("raw, value", [("", 30), ("  ", 30), ("0", 0), ("45", 45), ("36500", 36500)])
def test_days_are_a_whole_number_empty_is_30_and_0_is_off(raw, value):
    assert parse_days("PORTAL_REVIEW_DUE_SOON_DAYS", raw) == value


@pytest.mark.parametrize("raw", ["-1", "1.5", "thirty", "36501", "٣"])
def test_any_other_days_value_stops_with_the_setting_s_name_and_not_its_value(raw):
    with pytest.raises(BadSetting) as exc:
        parse_days("PORTAL_REREVIEW_STALE_DAYS", raw)
    assert "PORTAL_REREVIEW_STALE_DAYS" in str(exc.value) and raw not in str(exc.value)


def test_the_default_signals_are_the_portal_spec_s():
    assert parse_signals("") == DEFAULT_SIGNALS == frozenset({"security_review", "T-17", "T-18", "T-19", "egress-removed"})
    assert parse_signals(" T-19 , builder-bumped ,") == frozenset({"T-19", "builder-bumped"})


@pytest.mark.parametrize("raw", ["T-0", "T-1000", "t-17", "elevated", "egress_removed", "T-17;T-18"])
def test_an_unknown_signal_stops_with_the_setting_s_name(raw):
    with pytest.raises(BadSetting, match="PORTAL_REREVIEW_SIGNALS"):
        parse_signals(raw)


SIGNALS = ({"id": "T-2", "kind": "trigger", "level": "security_review", "summary": "s", "count": 1},
           {"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "s", "count": 1},
           {"id": "T-19", "kind": "trigger", "level": "elevated", "summary": "s", "count": 1},
           {"id": "builder-bumped", "kind": "recorded", "summary": "s"}, {"id": "egress-removed", "kind": "recorded", "summary": "s"})


def test_matching_takes_any_trigger_at_security_review_and_the_named_ids():
    assert matching(SIGNALS, DEFAULT_SIGNALS) == ("T-2", "T-17", "T-19", "egress-removed")
    assert matching(SIGNALS, frozenset({"T-19", "builder-bumped"})) == ("T-19", "builder-bumped")
    assert matching(SIGNALS[3:4], DEFAULT_SIGNALS) == ()


def item(opened, closed=None):
    return RereviewItem("i_" + "1" * 32, "acme/notes", "review_overdue", "2026-10-01", opened, closed_at=closed)


def test_an_item_is_stale_after_the_days_and_never_with_0():
    assert is_stale(item(NOW - dt.timedelta(days=31)), NOW, 30) and not is_stale(item(NOW - dt.timedelta(days=30)), NOW, 30)
    assert not is_stale(item(NOW - dt.timedelta(days=400)), NOW, 0)
    assert not is_stale(item(NOW - dt.timedelta(days=40), closed=NOW), NOW, 30)


def test_the_words_and_the_platform_s_notes_are_fixed():
    assert REASON_WORDS == {"review_overdue": "Review overdue", "review_due_soon": "Review due soon", "scorecard_signal": "Scorecard signal"}
    row = IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, "p_x", "low", "public", "p", dt.date(2027, 11, 1), None, None)
    soon = RereviewItem("i_" + "1" * 32, "acme/notes", "review_due_soon", "2026-11-01", NOW)
    assert auto_close_note(row, soon, TODAY, 30) == "Closed by the platform: the record's review date is now 2027-11-01."
    assert auto_close_note(None, soon, TODAY, 30) == "Closed by the platform: the app's intake record is gone."
    same = dc.replace(row, review_by=dt.date(2026, 11, 1))
    assert auto_close_note(same, soon, dt.date(2026, 11, 2), 30) == (
        "Closed by the platform: the review date 2026-11-01 has passed, so a Review overdue item replaces this one.")
    assert auto_close_note(same, soon, TODAY, 0) == "Closed by the platform: review-due-soon items are off (PORTAL_REVIEW_DUE_SOON_DAYS is 0)."
