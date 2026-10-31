"""The shell every page shares (spec 2026-10-30 R1, R10): a sidebar with the mark and the nav, the Admin heading with a count on
Approvals, the person and Sign out at the bottom, a skip link, and the content in <main>."""

import pytest
from portal_fakes import T0

COUNT = '<span class="nav-count">{n}<span class="visually-hidden"> waiting</span></span>'


def test_every_page_has_the_sidebar_the_skip_link_and_main(portal):
    error_page = f"/requests/{portal.seeds['request']}"   # not other's request: the error page, through base.html
    for who, path in (("anonymous", "/"), ("owner", "/me"), ("admin", "/admin/audit"), ("other", error_page)):
        text = portal.client(who).get(path).text
        assert '<a class="skip" href="#main">Skip to the content</a>' in text and '<aside class="sidebar">' in text
        assert '<main id="main" class="content">' in text and '<span class="brand-mark" aria-hidden="true"></span>Golden road' in text


def test_the_admin_sees_the_admin_heading_and_the_count_of_waiting_registrations(portal):
    text = portal.client("admin").get("/me").text
    assert '<p class="nav-heading" id="nav-admin">Admin</p>' in text and COUNT.format(n=1) in text
    portal.store.create_registration(person_id=portal.people["owner"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "Builds internal tools; no email.", "data_classification": "public"})
    assert COUNT.format(n=2) in portal.client("admin").get("/me").text


def test_others_see_no_admin_heading_and_no_count(portal):
    for who in ("anonymous", "owner"):
        text = portal.client(who).get("/").text
        assert "nav-heading" not in text and "nav-count" not in text


def test_the_person_and_sign_out_sit_at_the_bottom(portal):
    text = portal.client("owner").get("/me").text
    account = text[text.index('<div class="account">'):]
    assert '<p class="account-name">Owner</p>' in account and 'action="/logout"' in account and ">Sign out</button>" in account
    assert '<a class="button gold" href="/login">Sign in</a>' in portal.client().get("/").text


@pytest.mark.parametrize("path, key", [("/", "Start here"), ("/me", "Me"), ("/register", "Register"), ("/requests", "My requests")])
def test_the_current_page_is_marked_in_the_nav(portal, path, key):
    text = portal.client("owner").get(path).text
    assert f'aria-current="page"><span>{key}</span>' in text


def test_the_sidebar_runs_the_page_s_height_and_the_page_is_one_column(portal):
    """Reskin visual fixes: the dark sidebar's content is sticky inside an aside that stretches; the header, the content and the
    footer share one centred column."""
    text = portal.client("owner").get("/register").text
    assert '<aside class="sidebar">\n<div class="sidebar-inner">' in text
    main = text[text.index('<main id="main" class="content">'):]
    assert main.startswith('<main id="main" class="content">\n<div class="column">') and '<footer class="footer">' in main.split("</div>\n</main>")[0]


def test_the_count_leaves_out_the_admin_s_own_registrations(portal):
    portal.store.create_registration(person_id=portal.people["admin"].id, now=T0, draft={
        "schema": "registration-draft/v1", "repo": "acme/tools", "purpose": "An admin's own app; no email.", "data_classification": "public"})
    assert COUNT.format(n=1) in portal.client("admin").get("/me").text   # only owner's: another admin decides the admin's own


def test_a_store_error_while_counting_shows_no_count_rather_than_failing_the_page(portal, monkeypatch, caplog):
    import psycopg

    def broken(states):
        raise psycopg.OperationalError("the database is away")
    client = portal.client("admin")
    monkeypatch.setattr(portal.store, "requests_in", broken)
    response = client.get("/me")
    assert response.status_code == 200 and "nav-count" not in response.text
    assert any("approvals count" in r.getMessage() for r in caplog.records)


def test_a_bug_while_counting_is_not_hidden(portal, monkeypatch):
    def broken(states):
        raise TypeError("a bug")
    client = portal.client("admin")
    monkeypatch.setattr(portal.store, "requests_in", broken)
    with pytest.raises(TypeError):
        client.get("/me")


def test_the_rereview_page_is_marked_in_the_admin_nav(portal):
    assert 'aria-current="page"><span>Re-review</span>' in portal.client("admin").get("/admin/rereview").text
