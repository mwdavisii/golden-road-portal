"""The lifecycle outbox (spec 2026-11-02 L10, L11) as both the web and the collector read it: what a note says in ServiceNow (note_text,
the one place every note passes on its way to a task) and which unsent notes are stale (cmdb_writeback_pending on the Re-review page).
The web imports this module, never the collector's."""

import datetime as dt
import re
import unicodedata

STALE_AFTER = dt.timedelta(days=1)  # an unsent note older than this is cmdb_writeback_pending on the Re-review page
# ServiceNow renders what a journal field holds between [code] and [/code] as HTML: an admin's reason or an IdP display name in a note
# must never open one. A zero-width space after the "[" keeps the text readable and the tag inert.
CODE_TAG = re.compile(r"\[(\s*/?\s*code\s*)\]", re.IGNORECASE)
ZERO_WIDTH_SPACE = "\u200b"


def neutral(text: str) -> str:
    """The text with no control or format character but the newline (bidi overrides and isolates, zero-width characters and the like:
    a note can't be made to read other than it is), and no [code] or [/code] tag ServiceNow would render (any case, spaces inside)."""
    kept = "".join(c for c in text if c == "\n" or unicodedata.category(c) not in ("Cc", "Cf"))
    return CODE_TAG.sub(lambda m: f"[{ZERO_WIDTH_SPACE}{m.group(1)}]", kept)


def note_text(event, base_url: str) -> str:
    """What ServiceNow gets: the event's note, then the request's page in the portal, neutralised (every note passes here)."""
    return neutral(f"{event.note}\n\nGolden road portal request: {base_url.rstrip('/')}/requests/{event.request_id}")


def stale_events(store, now) -> list:
    """The unsent notes older than STALE_AFTER, oldest first."""
    return [e for e in store.lifecycle_events(unsent=True) if now - e.created_at > STALE_AFTER]
