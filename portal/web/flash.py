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
    "mcp_server": "MCP server requested",
    "skill": "Skill requested",
    "link_added": "GitHub login linked",
    "link_removed": "GitHub login unlinked",
    "lifecycle": "Lifecycle action requested: the writer applies it next",
    "retry": "Retry requested: the writer tries the step again on its next pass",
    "abandon": "Abandon requested: the writer closes the pull requests on its next pass",
    "stop": "Stop requested: the writer closes the steps still to do on its next pass",
    "service_principal": "Service principal requested: the collector creates it, or opens its ServiceNow task, on its next pass",
    "task_decided": "Decision stored: the collector reads the task on its next pass",
    "agent_saved": "Saved: the writer opens or updates the agent's pull request on its next pass",
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
