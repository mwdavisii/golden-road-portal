"""One-line confirmations after a POST redirects (spec 2026-10-30 R9; decision 10). The redirect sets a short-lived cookie that holds a
key from MESSAGES, never text from the request; the next page that renders shows that message once and clears the cookie. An unknown
value shows nothing."""

from fastapi.responses import RedirectResponse

from .cookies import set_cookie

FLASH_COOKIE = "__Host-portal-flash"
FLASH_SECONDS = 60
MESSAGES = {
    "registered": "Registration submitted for review",
    "rereview_closed": "Re-review item closed",
    "resubmitted": "Sent again for review",
    "approved": "Approved: the writer records it next",
    "sent_back": "Sent back to the requester",
    "connection": "Connection requested",
    "removal": "Removal requested",
    "secret": "Secret requested",
    "link_added": "GitHub login linked",
    "link_removed": "GitHub login unlinked",
}


def redirect(url: str, key: str) -> RedirectResponse:
    """A 303 to `url` that shows MESSAGES[key] on the next page."""
    if key not in MESSAGES:
        raise ValueError(f"no flash message {key!r}")
    response = RedirectResponse(url, status_code=303)
    set_cookie(response, FLASH_COOKIE, key, FLASH_SECONDS)
    return response


def message(request) -> str | None:
    return MESSAGES.get(request.cookies.get(FLASH_COOKIE, ""))
