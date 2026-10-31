"""Pages. Autoescape is on for every template, including .txt includes: text from people, GitHub or a draft is never HTML."""

import logging
from pathlib import Path

import jinja2
import psycopg
from fastapi.responses import HTMLResponse

from ..classification import LEVELS, MEANS
from ..rereview import REASON_WORDS
from ..timeline import STATUS_WORDS
from .cookies import clear_cookie
from .flash import FLASH_COOKIE, message

log = logging.getLogger("portal")

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True, undefined=jinja2.StrictUndefined)
# (level, its one line), least to most sensitive: the picker's cards (spec 2026-10-30 L3)
ENV.globals["CLASSIFICATION_LEVELS"] = tuple((level, MEANS[level]) for level in LEVELS)
ENV.globals["REASON_WORDS"] = REASON_WORDS  # a re-review item's reason in words
ENV.globals["STEP_WORDS"] = STATUS_WORDS  # a timeline step's status in words, beside its color (spec 2026-10-30 R10)
# A request's state in words, for its badge (the class keeps the state name: state-<state>)
ENV.globals["STATE_LABELS"] = {"pending": "Pending", "sent_back": "Sent back", "approved": "Approved", "writing": "Recording",
                               "pr_open": "Pull request open", "merged": "Merged", "failed": "Failed"}


def page(request, name: str, status: int = 200, **context) -> HTMLResponse:
    from .access import Principal
    principal = getattr(request.state, "principal", None) or Principal()
    state = request.app.state
    waiting = 0
    if principal.is_admin and state.store is not None:  # the count on Approvals: registrations this admin may decide
        try:
            waiting = sum(1 for r in state.store.requests_in(("pending",)) if r.kind == "registration" and r.requested_by != principal.person.id)
        except psycopg.Error as exc:  # the store's own failure: a count is not worth failing the page for; a bug still is
            log.warning("approvals count unavailable: %s", type(exc).__name__)
            waiting = 0
    rereview_open = 0
    if principal.is_admin and state.store is not None:  # the count on Re-review: open items
        try:
            rereview_open = len(state.store.rereview_items())
        except psycopg.Error as exc:
            log.warning("re-review count unavailable: %s", type(exc).__name__)
    shows_flash = request.method == "GET"  # a page rendered for a POST (a refusal) leaves the toast for the next page
    html = ENV.get_template(name).render(principal=principal, csrf=principal.csrf, settings=state.settings, approvals_waiting=waiting,
                                         rereview_open=rereview_open,
                                         flash=message(request) if shows_flash else None, **context)
    response = HTMLResponse(html, status_code=status)
    if shows_flash and FLASH_COOKIE in request.cookies:
        clear_cookie(response, FLASH_COOKIE)  # shown once (or unknown, and dropped)
    return response
