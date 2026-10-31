"""The re-review list's rules (spec 2026-10-31 R1 to R8), with no I/O: the reasons and their words, the settings' values, when a review
date gives a reason, which of an event's signals match, when an item is stale, and the notes the platform closes an item with. The
collector and the web both use them, so the list means the same thing everywhere."""

import datetime as dt
import re

REASON_WORDS = {"review_overdue": "Review overdue", "review_due_soon": "Review due soon", "scorecard_signal": "Scorecard signal"}
RECORDED = ("builder-bumped", "egress-removed", "resources-lowered")  # re-review-event/v1's recorded-only signals
DEFAULT_SIGNALS = frozenset({"security_review", "T-17", "T-18", "T-19", "egress-removed"})  # the portal spec's list (spec R5)
TRIGGER = re.compile(r"T-[1-9][0-9]{0,2}")
DEFAULT_DAYS, MAX_DAYS = 30, 36500  # MAX_DAYS keeps date arithmetic in range; it is not a budget


class BadSetting(ValueError):
    """A setting the collector or the web can't use. The message names the setting, never its value."""


def parse_days(name, raw) -> int:
    """A number of days: empty is 30, 0 turns the reason or the flag off (no budgets: 0 means off, not a limit)."""
    raw = (raw or "").strip()
    if raw == "":
        return DEFAULT_DAYS
    if not (raw.isascii() and raw.isdigit()) or int(raw) > MAX_DAYS:
        raise BadSetting(f"{name} must be a whole number of days from 0 (off) to {MAX_DAYS}")
    return int(raw)


def parse_signals(raw) -> frozenset:
    """PORTAL_REREVIEW_SIGNALS: trigger IDs, recorded-signal IDs and the word security_review, comma-separated; empty is the default."""
    items = [s.strip() for s in (raw or "").split(",") if s.strip()]
    if not items:
        return DEFAULT_SIGNALS
    if any(not (s == "security_review" or s in RECORDED or TRIGGER.fullmatch(s)) for s in items):
        raise BadSetting("PORTAL_REREVIEW_SIGNALS must list trigger IDs (T-17), recorded signals (builder-bumped, egress-removed, "
                         "resources-lowered) or security_review, separated by commas")
    return frozenset(items)


def date_reason(review_by, today, due_soon_days):
    """review_overdue when the date is before today (UTC); review_due_soon when it is within due_soon_days (0: never); else None."""
    if review_by is None:
        return None
    if review_by < today:
        return "review_overdue"
    if due_soon_days and (review_by - today).days <= due_soon_days:
        return "review_due_soon"
    return None


def matching(signals, wanted) -> tuple[str, ...]:
    """The IDs of an event's signals that match: named in `wanted`, or any trigger whose own level is security_review when `wanted` has
    the word security_review. In the event's order."""
    return tuple(s["id"] for s in signals if s["id"] in wanted
                 or ("security_review" in wanted and s.get("kind") == "trigger" and s.get("level") == "security_review"))


def is_stale(item, now, stale_days) -> bool:
    """The rereview_stale flag (spec R8): open more than stale_days (0: never)."""
    return bool(stale_days) and item.closed_at is None and now - item.opened_at > dt.timedelta(days=stale_days)


def auto_close_note(row, item, today, due_soon_days) -> str:
    """The note a date item closes with when its record no longer gives its reason with its date. Written here, from checked values
    only, and saying why: the record is gone, its date moved, the date passed (overdue replaces due soon), or due-soon is off."""
    if row is None:
        return "Closed by the platform: the app's intake record is gone."
    if row.review_by is None:
        return "Closed by the platform: the app's record has no review date now."
    if row.review_by.isoformat() != item.key:
        return f"Closed by the platform: the record's review date is now {row.review_by.isoformat()}."
    if date_reason(row.review_by, today, due_soon_days) == "review_overdue":
        return f"Closed by the platform: the review date {item.key} has passed, so a {REASON_WORDS['review_overdue']} item replaces this one."
    return "Closed by the platform: review-due-soon items are off (PORTAL_REVIEW_DUE_SOON_DAYS is 0)."
