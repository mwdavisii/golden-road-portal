"""The registration flow end to end on Postgres, each process as its own database role: web registers, decides and resubmits,
the collector records repo admins, the writer writes and merges. Every statement runs within migration 0002's rules and grants."""

import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient
from portal_fakes import BASE, ISSUER, T0, Clock, FakeGitHub, evidence_doc, make_settings, transport, writer_config
from portal_pg import pg_db, pg_server  # noqa: F401
from test_portal_writer_run import FILE, advancing, client, gh_for_write

from portal.roles import roles_for
from portal.store.base import RepoAdmin
from portal.store.postgres import PostgresStore
from portal.web.app import create_app
from portal.web.cookies import SESSION_COOKIE
from portal.writer.run import run_once

pytestmark = pytest.mark.docker


def test_registration_flow_on_postgres_within_the_database_rules(pg_db):
    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        settings, clock = make_settings(), Clock(T0)
        app = create_app(settings=settings, store=web, http=httpx.Client(transport=transport(FakeGitHub())), clock=clock)

        def person(name, groups):
            p, token, csrf = web.sign_in(issuer=ISSUER, subject=name, display_name=name.title(), email=None, groups=groups,
                                         roles=roles_for(groups, settings.role_groups), now=clock(), max_age=settings.session_max)
            http = TestClient(app, base_url=BASE, follow_redirects=False)
            http.cookies.set(SESSION_COOKIE, token, domain="portal.example.test")
            return p, http, csrf

        owner, owner_http, owner_csrf = person("owner", [])
        admin, admin_http, admin_csrf = person("admin", ["golden-road-admins"])
        web.add_link(person_id=owner.id, github_login="owner-gh", github_user_id=1001, source="admin", actor_id=admin.id,
                     reason="seeded for the flow test", now=clock())
        collector.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0), RepoAdmin("acme/tools", "owner-gh", 1001, T0)], observed_at=T0)

        form = {"csrf_token": owner_csrf, "repo": "acme/notes", "purpose": "Stores short team notes; no email, no files.",
                "data_classification": "public"}
        created = owner_http.post("/register", data=form)
        assert created.status_code == 303
        rid = created.headers["location"].rsplit("/", 1)[1]
        collector.add_evidence(request_id=rid, submission=1, commit="e" * 40, evidence=evidence_doc("acme/notes", "e" * 40), floor_tier="low",
                               floor_classification="public", rules_commit="f" * 40, now=T0)
        approval = {"csrf_token": admin_csrf, "submission": "1", "tier": "moderate", "data_classification": "public", "review_by": "2027-10-23",
                    "justification": "Purpose and classification checked."}
        assert admin_http.post(f"/admin/requests/{rid}/approve", data=approval).status_code == 303
        approved = web.get_request(rid)
        assert approved.state == "approved" and approved.decision_groups == ("golden-road-admins",)

        # the writer, as its own role: claim, write the PR, wait for the check, merge
        run_once(writer, client(gh_for_write()), writer_config(), T0)
        assert web.get_request(rid).state == "pr_open"
        gh = advancing(req=web.get_request(rid))
        run_once(writer, client(gh), writer_config(), T0)
        merged = web.get_request(rid)
        assert (merged.state, merged.merge_sha, merged.pr_number) == ("merged", "3" * 40, 12)
        assert FILE.endswith("intake/acme/notes.yaml")

        # a second repo is sent back and resubmitted by its requester: the web role's own transitions
        form2 = {**form, "repo": "acme/tools"}
        rid2 = owner_http.post("/register", data=form2).headers["location"].rsplit("/", 1)[1]
        assert admin_http.post(f"/admin/requests/{rid2}/send-back", data={"csrf_token": admin_csrf, "justification": "Say what it does not do."}).status_code == 303
        again = {"csrf_token": owner_csrf, "purpose": "Builds internal tools; no PHI, no email, no files.", "data_classification": "public"}
        assert owner_http.post(f"/requests/{rid2}", data=again).status_code == 303
        assert web.get_request(rid2).state == "pending" and web.get_request(rid2).decided_by is None

        actions = [(e.actor_kind, e.action) for e in reversed(web.audit_entries(1000)) if e.subject_id == rid]
        assert actions == [("person", "request.created"), ("person", "request.approved"), ("writer", "request.writing"),
                           ("writer", "request.pr_open"), ("writer", "request.merged")]
        assert all(abs((dt.datetime.now(dt.UTC) - e.at).total_seconds()) < 600 for e in web.audit_entries(1000)), "the database sets the time"
    finally:
        for s in (web, writer, collector):
            s.close()


def test_the_writer_cannot_touch_a_request_the_web_has_not_approved(pg_db):
    web, writer = PostgresStore(pg_db("portal_web")), PostgresStore(pg_db("portal_writer"))
    try:
        owner, _, _ = web.sign_in(issuer="i", subject="o", display_name="O", email=None, groups=[], roles=[], now=T0, max_age=dt.timedelta(hours=8))
        web.create_registration(person_id=owner.id, draft={"schema": "registration-draft/v1", "repo": "acme/notes"}, now=T0)
        assert writer.claim_approved(now=T0) is None and writer.requests_in(("writing",)) == []
    finally:
        web.close()
        writer.close()


def test_a_binding_is_written_and_merged_by_the_writer_s_own_role(pg_db):
    """The writer re-checks a binding's requester against identity_link and repo_admin as its own role, then writes the
    environment's bindings.yaml and composed.yaml in golden-road-deploy and merges."""
    from portal.binding_record import next_binding
    from portal.ids import new_id
    from portal_fakes import fake_directory
    from test_portal_writer_run import badvancing, bcfg, bgh_for_write

    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        groups = []
        owner, _, _ = web.sign_in(issuer=ISSUER, subject="owner", display_name="Owner", email=None, groups=groups, roles=frozenset(),
                                  now=T0, max_age=dt.timedelta(hours=8))
        admin, _, _ = web.sign_in(issuer=ISSUER, subject="admin", display_name="Admin", email=None, groups=["golden-road-admins"],
                                  roles=frozenset({"admin"}), now=T0, max_age=dt.timedelta(hours=8))
        web.add_link(person_id=owner.id, github_login="owner-gh", github_user_id=1001, source="admin", actor_id=admin.id,
                     reason="seeded for the flow test", now=T0)
        collector.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0)], observed_at=T0)
        rid = new_id("r")
        doc = next_binding(current=None, repo="acme/notes", org="acme", directory=fake_directory(), person_id=owner.id, request_id=rid,
                           now=T0, environment="test", principal_id="sp-notes")
        web.create_binding(request_id=rid, person_id=owner.id, final=doc, now=T0)

        run_once(writer, client(bgh_for_write()), bcfg(), T0)
        assert web.get_request(rid).state == "pr_open", web.get_request(rid).failure_reason
        run_once(writer, client(badvancing(web.get_request(rid))), bcfg(), T0)
        assert web.get_request(rid).state == "merged"

        # the next change: the collector no longer sees the owner as an admin, so the writer fails it before any GitHub call
        collector.replace_repo_admins([], observed_at=T0)
        rid2 = new_id("r")
        doc2 = next_binding(current=doc, repo="acme/notes", org="acme", directory=fake_directory(), person_id=owner.id, request_id=rid2,
                            now=T0, environment="test", warehouse_id="wh-notes")
        web.create_binding(request_id=rid2, person_id=owner.id, final=doc2, now=T0, current=doc)
        gh = FakeGitHub()
        run_once(writer, client(gh), bcfg(), T0)
        assert (web.get_request(rid2).state, web.get_request(rid2).failure_reason) == ("failed", "recheck: requester_not_admin") and gh.calls == []
    finally:
        for s in (web, writer, collector):
            s.close()
