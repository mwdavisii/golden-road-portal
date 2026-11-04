"""The registration flow end to end on Postgres, each process as its own database role: web registers, decides and resubmits,
the collector records repo admins, the writer writes and merges. Every statement runs within migration 0002's rules and grants."""

import datetime as dt

import httpx
import psycopg
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
                "data_classification": "public", "ownership_acknowledged": "yes"}
        created = owner_http.post("/register", data=form)
        assert created.status_code == 303
        rid = created.headers["location"].rsplit("/", 1)[1]
        assert web.get_request(rid).ownership_acknowledged_at is not None   # the register form's acknowledgement (0011)
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
        assert web.get_request(rid2).ownership_acknowledged_at is not None   # a resubmit keeps the acknowledgement

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


def test_lifecycle_flow_on_postgres_within_the_database_rules(pg_db):
    """Spec 2026-11-02: the web records lifecycle requests, the writer drives them as its own role (a suspend and a resume merged by
    the writer, a decommission's deploy PR merged by a person, the repository archived by the lifecycle App, the factory PR merged by a person), and
    meanwhile the writer refuses a binding opened before the decommission (item 15)."""
    from portal.binding_record import next_binding
    from portal.ids import new_id
    from portal_fakes import RepoWorld, deploy_world, fake_directory, lifecycle_app, lifecycle_request

    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        owner, _, _ = web.sign_in(issuer=ISSUER, subject="owner", display_name="Owner", email=None, groups=[], roles=frozenset(),
                                  now=T0, max_age=dt.timedelta(hours=8))
        admin, _, _ = web.sign_in(issuer=ISSUER, subject="admin", display_name="Admin", email=None, groups=["golden-road-admins"],
                                  roles=frozenset({"admin"}), now=T0, max_age=dt.timedelta(hours=8))
        people = {"admin": admin}
        world = RepoWorld()
        deploy, factory = deploy_world(world)
        world.installed["acme/notes"] = 77
        gh = client(world)
        cfg = writer_config(directory=fake_directory(), lifecycle_app=lifecycle_app(world))

        def passes(n):
            for _ in range(n):
                run_once(writer, gh, cfg, T0)

        for action, task in (("suspend", "SCTASK0010001"), ("resume", "SCTASK0010002")):
            req = lifecycle_request(web, people, T0, action=action, task=task, claim=False)
            passes(3)
            assert web.get_request(req.id).state == "merged", writer.lifecycle_steps(req.id)
            assert [e.step for e in web.lifecycle_events(req.id)] == ["requested", "pr_opened", "merged", "completed"]
        assert web.lifecycle_state("acme/notes").state == "active"

        web.add_link(person_id=owner.id, github_login="owner-gh", github_user_id=1001, source="admin", actor_id=admin.id,
                     reason="seeded for the flow test", now=T0)
        collector.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0)], observed_at=T0)
        life = lifecycle_request(web, people, T0, action="decommission", task="SCTASK0010003", claim=False)
        passes(1)  # the deploy PR opens
        rid = new_id("r")  # a binding opened while the decommission's deploy PR waits for its review
        web.create_binding(request_id=rid, person_id=owner.id, now=T0, final=next_binding(
            current=None, repo="acme/notes", org="acme", directory=fake_directory(), person_id=owner.id, request_id=rid, now=T0,
            environment="test", principal_id="sp-notes"))
        assert web.get_request(rid).state == "approved"
        world.merge(deploy, 3)
        before = {req.id: req.state for req in web.requests_in(("approved", "writing"))}
        assert before == {life.id: "writing", rid: "approved"}
        passes(1)  # the deploy step done (decommissioned), and the binding claimed and refused in the same pass
        assert web.lifecycle_state("acme/notes").state == "decommissioned"
        assert (web.get_request(rid).state, web.get_request(rid).failure_reason) == ("failed", "recheck: decommissioned")
        passes(1)  # the archive
        assert world.archived == {"acme/notes"} and [s.state for s in writer.lifecycle_steps(life.id)] == ["done", "done", "planned"]
        passes(1)  # the factory PR
        world.merge(factory, 1)
        passes(2)
        assert web.get_request(life.id).state == "merged"
        assert [e.step for e in web.lifecycle_events(life.id)] == [
            "requested", "pr_opened", "merged", "archived", "pr_opened", "factory_merged", "completed"]
    finally:
        for s in (web, writer, collector):
            s.close()


def test_lifecycle_retry_and_abandon_on_postgres_within_the_database_rules(pg_db):
    """Rules 7 and 8 as the roles hold them: the web asks for a retry and an abandon, the writer carries them out (a step back to planned
    on a new attempt, steps closed, the request failed, each command marked done, its notes)."""
    from portal_fakes import RepoWorld, deploy_world, lifecycle_app, lifecycle_request
    web, writer = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer"))
    try:
        admin, _, _ = web.sign_in(issuer=ISSUER, subject="admin", display_name="Admin", email=None, groups=["golden-road-admins"],
                                  roles=frozenset({"admin"}), now=T0, max_age=dt.timedelta(hours=8))
        world = RepoWorld()
        deploy, _ = deploy_world(world)
        world.installed["acme/notes"] = 77
        gh, cfg = client(world), writer_config(lifecycle_app=lifecycle_app(world))

        def passes(n):
            return [line for _ in range(n) for line in run_once(writer, gh, cfg, T0)]

        world.checks[(deploy, "deploy-check")] = "failure"
        suspend = lifecycle_request(web, {"admin": admin}, T0, claim=False)
        passes(2)
        assert writer.lifecycle_steps(suspend.id)[0].state == "failed"
        world.checks.clear()
        web.add_lifecycle_command(request_id=suspend.id, command="retry", actor_id=admin.id, now=T0)
        passes(3)
        assert web.get_request(suspend.id).state == "merged" and web.open_lifecycle_command(suspend.id) is None
        assert writer.lifecycle_steps(suspend.id)[0].attempt == 2
        assert [e.step for e in web.lifecycle_events(suspend.id)] == ["requested", "pr_opened", "failed", "retried", "pr_opened", "merged",
                                                                      "completed"]

        life = lifecycle_request(web, {"admin": admin}, T0, action="decommission", task="SCTASK0010003", claim=False)
        passes(1)  # the deploy PR opens and waits for a person
        web.add_lifecycle_command(request_id=life.id, command="abandon", actor_id=admin.id, now=T0)
        assert passes(1) == [f"{life.id}: abandoned"]
        after = web.get_request(life.id)
        assert (after.state, after.failure_reason) == ("failed", "abandoned") and web.open_lifecycle_command(life.id) is None
        assert [s.state for s in writer.lifecycle_steps(life.id)] == ["closed"] * 3
        assert [e.step for e in web.lifecycle_events(life.id)][-1] == "abandoned" and world.archived == set()
        assert web.lifecycle_state("acme/notes").state == "suspended", "nothing of the decommission applied"
    finally:
        for s in (web, writer):
            s.close()


def test_lifecycle_stop_on_postgres_within_the_database_rules(pg_db):
    """I-1 as the roles hold it: an archive that can't finish, a Stop asked by the web and carried out by the writer (the steps left
    closed, the request failed "stopped", its note), and then procedure 18's bring-back, which 0012 takes only after the stop."""
    from portal_fakes import RepoWorld, deploy_world, lifecycle_app, lifecycle_request
    web, writer = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer"))
    try:
        admin, _, _ = web.sign_in(issuer=ISSUER, subject="admin", display_name="Admin", email=None, groups=["golden-road-admins"],
                                  roles=frozenset({"admin"}), now=T0, max_age=dt.timedelta(hours=8))
        world = RepoWorld()
        deploy, _ = deploy_world(world)  # the lifecycle App is not installed on acme/notes: the archive fails
        gh, cfg = client(world), writer_config(lifecycle_app=lifecycle_app(world))

        def passes(n):
            return [line for _ in range(n) for line in run_once(writer, gh, cfg, T0)]

        life = lifecycle_request(web, {"admin": admin}, T0, action="decommission", task="SCTASK0010003", claim=False)
        passes(1)
        world.merge(deploy, 1)
        passes(2)
        assert [s.state for s in writer.lifecycle_steps(life.id)] == ["done", "failed", "planned"]
        owner = psycopg.connect(pg_db("portal_owner"), autocommit=True)
        try:
            bring_back = "UPDATE lifecycle_state SET state = 'active', task = NULL WHERE lower(repo) = 'acme/notes'"
            with pytest.raises(psycopg.errors.RaiseException, match="applied in git or stopped"):
                owner.execute(bring_back)
            web.add_lifecycle_command(request_id=life.id, command="stop", actor_id=admin.id, now=T0)
            assert passes(1) == [f"{life.id}: stopped"]
            after = web.get_request(life.id)
            assert (after.state, after.failure_reason) == ("failed", "stopped") and web.open_lifecycle_command(life.id) is None
            assert [s.state for s in writer.lifecycle_steps(life.id)] == ["done", "closed", "closed"]
            assert [e.step for e in web.lifecycle_events(life.id)][-1] == "stopped" and world.archived == set()
            assert web.lifecycle_state("acme/notes").state == "decommissioned"
            assert owner.execute(bring_back).rowcount == 1
        finally:
            owner.close()
        assert web.lifecycle_state("acme/notes").state == "active"
    finally:
        for s in (web, writer):
            s.close()


def test_the_lifecycle_pages_on_postgres_as_the_web_s_role(pg_db):
    """Spec 2026-11-02 "The portal" as the web role holds it: the admin's suspend form (the task checked against a fresh mock with the
    sent notes replayed), the app page's panel and banner, the request page with its outbox, Retry, and the mock task page."""
    import dataclasses
    from portal_fakes import RepoWorld, deploy_world, lifecycle_app, mock_servicenow
    from portal.servicenow_mock import ci_sys_id
    from portal.store.base import IntakeObserved

    web, writer, collector = (PostgresStore(pg_db(r)) for r in ("portal_web", "portal_writer", "portal_collector"))
    try:
        settings, clock = dataclasses.replace(make_settings(), servicenow=True, servicenow_api=mock_servicenow()), Clock(T0)
        app = create_app(settings=settings, store=web, http=httpx.Client(transport=transport(FakeGitHub())), clock=clock)
        admin, token, csrf = web.sign_in(issuer=ISSUER, subject="admin", display_name="Admin", email=None, groups=["golden-road-admins"],
                                         roles=frozenset({"admin"}), now=T0, max_age=dt.timedelta(hours=8))
        http = TestClient(app, base_url=BASE, follow_redirects=False)
        http.cookies.set(SESSION_COOKIE, token, domain="portal.example.test")
        collector.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, T0)], observed_at=T0)
        collector.replace_intake_observed([IntakeObserved("acme/notes", "c" * 40, "intake/v2", True, "platform-team", "low", "public",
                                                          "Stores notes.", dt.date(2027, 10, 23), None, None)], observed_at=T0)
        collector.record_cmdb_sync(repo="acme/notes", sys_id=ci_sys_id("acme/notes"), number="APM0001001", fields_hash="f" * 64,
                                   operation="INSERT", now=T0)
        app_page = http.get("/apps/acme/notes").text
        assert '<span class="badge">Active</span>' in app_page and 'href="/apps/acme/notes/lifecycle/suspend"' in app_page

        world = RepoWorld()
        deploy, _ = deploy_world(world)
        world.checks[(deploy, "deploy-check")] = "failure"
        gh, cfg = client(world), writer_config(lifecycle_app=lifecycle_app(world))
        asked = http.post("/apps/acme/notes/lifecycle/suspend", data={"csrf_token": csrf, "task": "SCTASK0010001",
                                                                     "reason": "Critical vulnerability CVE-2026-0001; suspended until patched"})
        assert asked.status_code == 303
        rid = asked.headers["location"].rsplit("/", 1)[1]
        for _ in range(2):
            run_once(writer, gh, cfg, T0)
        assert writer.lifecycle_steps(rid)[0].state == "failed"
        for e in collector.lifecycle_events(rid, unsent=True)[:2]:
            collector.mark_lifecycle_event_sent(event_id=e.id, now=T0)
        page = http.get(f"/requests/{rid}").text
        assert "What ServiceNow was told" in page and "Sent at <time" in page and f'action="/requests/{rid}/retry"' in page
        task = http.get("/mock/servicenow/tasks/SCTASK0010001").text
        assert task.count("<h3>Work note</h3>") == 2 and '<span class="badge">Open</span>' in task
        assert http.post(f"/requests/{rid}/retry", data={"csrf_token": csrf}).status_code == 303
        assert web.open_lifecycle_command(rid).command == "retry"
        assert "Retry asked by Admin at <time" in http.get(f"/requests/{rid}").text
        assert "A lifecycle request is open" in http.get("/apps/acme/notes").text
    finally:
        for s in (web, writer, collector):
            s.close()
