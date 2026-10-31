"""Every page renders under the CSP (style-src 'self', script-src 'self', spec 2026-10-30 R and Tests): no style attribute, no <style>
element, no inline event handler, no javascript: URL and no <script> with a body, in the templates and in what the pages render; every
script is a file the portal serves."""

import datetime as dt
import re
from html import unescape
from pathlib import Path

import pytest

PORTAL_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = PORTAL_DIR / "portal/templates"
STATIC = PORTAL_DIR / "portal/static"
STYLE_ATTR = re.compile(r"<[a-zA-Z][^>]*\sstyle\s*=", re.S)
HANDLER = re.compile(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", re.S)
STYLE_TAG = re.compile(r"<style\b", re.I)
JS_URL = re.compile(r"""(href|src|action)\s*=\s*["']?\s*javascript:""", re.I)
SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.I | re.S)


JINJA = re.compile(r"{{.*?}}|{%.*?%}|{#.*?#}", re.S)
SCRIPT_OPEN = re.compile(r"<script\b([^>]*)>", re.I)


def problems(html: str) -> list[str]:
    """What breaks the CSP in a template or a page. Jinja tags are blanked first, so a ">" inside one can't end a tag early, and
    entities are decoded for the javascript: check."""
    html = JINJA.sub(" ", html)
    found = [name for name, rx in (("a style attribute", STYLE_ATTR), ("an inline handler", HANDLER), ("a <style> element", STYLE_TAG))
             if rx.search(html)]
    if JS_URL.search(html) or JS_URL.search(unescape(html)):
        found.append("a javascript: URL")
    for opened in SCRIPT_OPEN.finditer(html):
        rest = html[opened.end():]
        closed = re.match(r"(.*?)</script\s*>", rest, re.I | re.S)
        src = re.search(r'\bsrc="/static/([A-Za-z0-9_.-]+)"', opened.group(1))
        if closed is None or closed.group(1).strip() or not src:
            found.append("an inline script")
        elif not (STATIC / src.group(1)).is_file():
            found.append(f"a missing script {src.group(1)}")
    return found


@pytest.mark.parametrize("html", [
    '<p style="color:red">x</p>', "<p\nstyle='x'>", '<div class="a" onclick="go()">', '<a {% if a > b %}onclick="x"{% endif %}>y</a>',
    '<img src=x onerror=alert(1)>', '<a href="javascript:alert(1)">', '<a href="&#106;avascript:alert(1)">', '<a href="javascript&colon;x">',
    '<a href=" JavaScript:x">', "<script>alert(1)</script>", '<script src="/static/forms.js">alert(1)</script>',
    '<script src="/static/forms.js" defer>', "<script>alert(1)", '<script src="https://cdn.example.org/x.js"></script>',
    '<script src="/static/nope.js"></script>', "<style>p{}</style>", "<STYLE>p{}</STYLE>"])
def test_the_scan_catches_what_breaks_the_csp(html):
    assert problems(html) != [], html


@pytest.mark.parametrize("html", ['<script src="/static/forms.js" defer></script>', '<p class="style">on air</p>', '<a href="/x">javascript</a>',
                                  '{% if a > b %}<p>x</p>{% endif %}'])
def test_the_scan_leaves_fine_markup_alone(html):
    assert problems(html) == [], html


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.html")), ids=lambda p: p.name)
def test_no_template_has_inline_style_or_script(path):
    assert problems(path.read_text(encoding="utf-8")) == []


# (who, path) for every page the portal renders, with data on it. Later tasks append the pages they add.
PAGES = [("anonymous", "/"), ("anonymous", "/register"), ("owner", "/"), ("owner", "/me"), ("owner", "/register"), ("owner", "/requests"), ("owner", "/requests?filter=attention"),
         ("owner", "/requests/{request}"), ("owner", "/apps/acme/notes"), ("owner", "/apps/acme/notes?environment=prod"),
         ("admin", "/admin/approvals"), ("admin", "/admin/people"), ("admin", "/admin/audit"), ("admin", "/admin/directory"),
         ("admin", "/apps/acme/notes"), ("admin", "/requests/{request}"), ("owner", "/requests/{sent_back}"),
         ("owner", "/apps/acme/notes?confirm=azure-identity"), ("owner", "/apps/acme/notes?environment=test&confirm=azure-identity"),
         ("owner", "/apps/acme/notes?flash=connection"),
         ("owner", "/apps/acme/notes/secrets/new"), ("owner", "/apps/acme/notes/secrets/new?folder=17"),   # the secrets steps 1 to 4
         ("owner", "/apps/acme/notes/secrets/new?folder=17&secret_id=1042"),
         ("owner", "/apps/acme/notes/secrets/new?folder=17&secret_id=1042&field_slug=password&env=NOTES_DB_PASSWORD"),
         ("owner", "/apps/acme/notes/secrets/new?folder=30"),   # a refused step
         ("admin", "/admin/people?flash=link_added"),   # the toast after a redirect (the flash cookie, not the query, carries it)
         ("owner", "/register?repo=acme/tools"), ("owner", "/register?repo=Acme/Tools"),   # change or renew: a v2 record, a v1 one
         ("admin", "/admin/approvals?change=1"), ("admin", "/admin/approvals?renewal=1"), ("owner", "/requests/{change}"),
         ("admin", "/admin/rereview"), ("owner", "/apps/acme/notes?banner=1"),   # the page ignores the query: it only names this case
         ("other", "/requests/{request}")]   # the last is the error page (404, through base.html)


# What each set-up page must show, so the scan looks at the page it means to (the step, the card, the refusal)
STEP = 'aria-current="step"><span class="step-dot" aria-hidden="true">{}</span>'
NEW = "/apps/acme/notes/secrets/new"
RENDERS = {"/apps/acme/notes?confirm=azure-identity": "Remove the Azure identity?",
           "/apps/acme/notes?environment=test&confirm=azure-identity": "Remove the Azure identity?",
           NEW: STEP.format(1), f"{NEW}?folder=17": STEP.format(2), f"{NEW}?folder=17&secret_id=1042": STEP.format(3),
           f"{NEW}?folder=17&secret_id=1042&field_slug=password&env=NOTES_DB_PASSWORD": STEP.format(4),
           f"{NEW}?folder=30": "Choose a folder from the list.", "/requests/{sent_back}": "What the admin asked you to change",
           "/apps/acme/notes?flash=connection": '<p class="toast" role="status">Connection requested</p>',
           "/admin/people": '<section class="card person">', "/admin/people?flash=link_added": '<p class="toast" role="status">GitHub login linked</p>',
           "/admin/audit": '<th scope="col">When</th>', "/admin/directory": '<th scope="col">Client ID</th>',
           "/register?repo=acme/tools": "<h1>Change or renew the registration</h1>",
           "/register?repo=Acme/Tools": "<dd>none (registered outside the portal)</dd>",
           "/admin/approvals?change=1": "Owner changes from platform-team to", "/admin/approvals?renewal=1": "A renewal of the current registration",
           "/requests/{change}": "A change to the current registration", "/admin/rereview": '<span class="badge">Scorecard signal</span>',
           "/apps/acme/notes?banner=1": "Change or renew the registration"}


def prepare(portal, path):
    """The data a page needs before it renders: a sent-back request, Secret Server for the secrets steps, or a linked identity to
    confirm removing."""
    if "{sent_back}" in path:   # the seeded request, sent back: the requester's resubmit form and the admin's note
        portal.store.decide(request_id=portal.seeds["request"], actor_id=portal.people["admin"].id, actor_groups=["golden-road-admins"],
                            approve=False, note="Say what it does not do.", final=None, now=portal.clock())
    if "secrets/new" in path:
        from test_portal_apps import with_secret_server
        with_secret_server(portal)
    if path == "/register?repo=acme/tools":   # a portal-written intake/v2 record
        from test_portal_register_change import observe
        observe(portal)
    if path == "/register?repo=Acme/Tools":   # an intake/v1 record (and the repo named in another case)
        from test_portal_register_change import observe
        observe(portal, schema="intake/v1", by_portal=False, owner="platform-team", review_by=None)
    if path in ("/admin/approvals?change=1", "/requests/{change}"):   # the query is ignored by the page; it only names this case
        from test_portal_register_change import pending_change, v1_record
        portal.seeds["change"] = pending_change(portal, v1_record()).id   # a v1 record, another owner: a change of owner too
    if path == "/admin/rereview":   # each reason, a stale item, the sources' problems and the records that are not items
        from test_portal_rereview_page import observe, overdue, signal
        from portal.store.base import IntakeObserved
        observe(portal, extra=[IntakeObserved("acme/broken", "e" * 40, None, False, None, None, None, None, None, None, "case_mismatch")])
        overdue(portal, opened=portal.clock() - dt.timedelta(days=40))
        signal(portal)
        portal.store.set_rereview_status(source="events", problem="actions_unreadable", counts={}, now=portal.clock())
    if path == "/apps/acme/notes?banner=1":
        from test_portal_apps import overdue_record
        overdue_record(portal)
    if path == "/admin/approvals?renewal=1":
        from portal_fakes import record_view
        from test_portal_register_change import pending_change
        pending_change(portal, record_view(portal.people["owner"].id))
    if "confirm=" in path:
        from test_portal_apps import link_identity_and_merge
        link_identity_and_merge(portal)


@pytest.mark.parametrize("who, path", PAGES)
def test_every_rendered_page_has_no_inline_style_or_script(portal, who, path):
    prepare(portal, path)
    client = portal.client(who)
    if "flash=" in path:
        from portal.web.flash import FLASH_COOKIE
        client.cookies.set(FLASH_COOKIE, path.split("flash=", 1)[1], domain="portal.example.test")
    response = client.get(path.format(request=portal.seeds["request"], sent_back=portal.seeds["request"], change=portal.seeds.get("change")))
    assert RENDERS.get(path, "") in response.text, (path, RENDERS.get(path))
    flash_set = [c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-portal-flash=")]
    assert all("Max-Age=0" in c for c in flash_set), flash_set   # a GET page only ever clears the toast, never sets one
    assert response.status_code < 500 and response.headers["content-type"].startswith("text/html")
    assert problems(response.text) == [], (who, path)
