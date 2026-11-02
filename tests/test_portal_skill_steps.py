"""The skills picker in steps (spec 2026-10-31 Part 2, "The portal"): Environment, Skill (the catalog's skills approved for the
environment and the person's sign-in groups), Review and save; Update re-pins a bound skill to the catalog's current main commit, and
Remove asks first. Every step checks the earlier ones again; only a POST creates a binding request, and it checks everything again.

The mock catalog's main has incident-triage (test, prod; everyone), release-notes (test only) and finance-close (prod; the
finance-analysts group only); its older commit OLD has incident-triage only."""

import html
import json
import re

import pytest
from portal_fakes import ISSUER, binding_request, make_settings
from test_portal_apps import bindings, merge_next, post

from portal import skills_catalog
from portal.roles import roles_for
from portal.web.flash import FLASH_COOKIE

NEW = "/apps/acme/notes/skills/new"
OLD = "2c7d1e9f0a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d"
MAIN = "6b1f0c2d9e8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"


def with_catalog(portal, catalog=None, **settings):
    portal.app.state.settings = make_settings(directory=portal.settings.directory, skills_catalog=catalog or skills_catalog.load_mock(),
                                              **settings)
    return portal


@pytest.fixture
def skills(portal):
    return with_catalog(portal)


def in_groups(portal, groups, who="owner"):
    """Sign `who` in again with these groups: the session holds the groups claim the skills' audience is matched against."""
    s = portal.settings
    _, token, csrf = portal.store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=f"{who}@example.test",
                                          groups=groups, roles=roles_for(groups, s.role_groups), now=portal.clock(), max_age=s.session_max)
    portal.tokens[who], portal.csrfs[who] = token, csrf
    return portal


def get(portal, who="owner", **params):
    return portal.client(who).get(NEW, params=params)


def radios(text, name):
    return set(re.findall(rf'<input type="radio" name="{name}" value="([^"]+)"', text))


def step_of(text):
    return re.search(r'aria-current="step"><span class="step-dot" aria-hidden="true">(\d)</span>', text).group(1)


def add(portal, name="incident-triage", who="owner", **form):
    return post(portal, who, "/apps/acme/notes/skills", name=name, **form)


def add_and_merge(portal, name="incident-triage", **form):
    assert add(portal, name, **form).status_code == 303
    portal.clock.advance(seconds=1)
    return merge_next(portal)


def pinned_old_and_merged(portal):
    """incident-triage in test pinned at OLD, recorded (merged) the way the writer records it."""
    req = binding_request(portal.store, portal.people, portal.clock(), identity=False, skills=[("incident-triage", OLD)])
    store, now = portal.store, portal.clock()
    store.mark_pr_open(request_id=req.id, branch=f"portal/{req.id}", pr_number=7, pr_url="https://github.com/acme/x/pull/7",
                       head_sha="2" * 40, base_sha="1" * 40, now=now)
    store.mark_merged(request_id=req.id, merge_sha="3" * 40, now=now)
    portal.clock.advance(seconds=1)


def test_the_section_is_hidden_and_the_steps_are_404_without_a_catalog(portal):
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Skills (platform catalog)" not in page and "/skills/new" not in page
    assert get(portal).status_code == 404 and get(portal, environment="test").status_code == 404
    response = add(portal)
    assert response.status_code == 400 and "The skills catalog is not set up here." in response.text and not bindings(portal)


def test_the_app_page_links_to_the_steps(skills):
    page = skills.client("owner").get("/apps/acme/notes").text
    assert "Skills (platform catalog)" in page and "No skills in test yet." in page
    assert '<a class="button" href="/apps/acme/notes/skills/new?environment=test">Add a skill</a>' in page


def test_step_1_offers_both_environments_with_the_tab_pre_chosen(skills):
    text = get(skills).text
    assert step_of(text) == "1" and radios(text, "environment") == {"test", "prod"} and 'value="test" required checked>' in text
    assert 'value="prod" required checked>' in get(skills, tab="prod").text


def test_step_2_offers_what_the_environment_and_the_person_may_have(skills):
    text = get(skills, environment="test").text
    assert step_of(text) == "2" and radios(text, "name") == {"incident-triage", "release-notes"}
    assert "Triage an incident report into severity, owner and next step." in text and f'Approved at <span class="mono">{MAIN[:7]}</span>' in text
    assert radios(get(skills, environment="prod").text, "name") == {"incident-triage"}
    in_groups(skills, ["finance-analysts"])
    assert radios(get(skills, environment="prod").text, "name") == {"incident-triage", "finance-close"}


def test_a_bound_skill_is_marked_and_points_at_update(skills):
    add_and_merge(skills)
    text = get(skills, environment="test").text
    assert radios(text, "name") == {"release-notes"} and "Already on this app" in text and "update it from the app's page" in text


@pytest.mark.parametrize("params, message, step", [
    ({"name": "incident-triage"}, "Choose an environment first.", "1"),
    ({"environment": "test", "name": "Incident"}, "Choose a skill from the list.", "2"),
    ({"environment": "test", "name": "nope"}, "That skill is not in the catalog.", "2"),
    ({"environment": "prod", "name": "release-notes"}, "That skill is not approved for prod.", "2"),
    ({"environment": "prod", "name": "finance-close"}, "That skill is for some groups only, and you are not in one.", "2"),
])
def test_a_skipped_or_tampered_choice_is_refused(skills, params, message, step):
    response = get(skills, **params)
    assert response.status_code == 400 and message in html.unescape(response.text) and step_of(response.text) == step
    assert not bindings(skills)


def test_a_bad_environment_is_a_400(skills):
    assert get(skills, environment="staging").status_code == 400 and get(skills, tab="staging").status_code == 400


def test_step_3_lists_the_files_and_only_the_post_creates_the_request(skills):
    text = get(skills, environment="test", name="incident-triage").text
    assert step_of(text) == "3" and not bindings(skills)
    assert "/skills/incident-triage/SKILL.md" in text and "/skills/incident-triage/checklists/sev1.md" in text and "skill.yaml" not in text
    assert MAIN in text and "bytes in all." in text
    for name, value in (("environment", "test"), ("name", "incident-triage")):
        assert f'<input type="hidden" name="{name}" value="{value}">' in text
    response = add(skills)
    assert response.status_code == 303 and response.headers["location"] == "/apps/acme/notes?environment=test"
    assert any(c.startswith(f"{FLASH_COOKIE}=skill") for c in response.headers.get_list("set-cookie"))
    [req] = bindings(skills)
    assert req.state == "approved" and req.final["skills"] == [{"name": "incident-triage", "commit": MAIN, "verified_by": "mock-skills-catalog"}]


def test_the_app_page_shows_the_skill_and_what_the_container_gets(skills):
    add_and_merge(skills)
    page = skills.client("owner").get("/apps/acme/notes").text
    assert f'<td class="mono">incident-triage</td><td class="mono">{MAIN[:7]}</td><td>the same</td>' in page
    assert "/skills/update" not in page and "confirm_skill=incident-triage" in page   # at main: no Update
    assert '<td class="mono">SKILLS_DIR</td><td class="mono">/skills</td>' in page
    assert f"mounted read-only from the platform's skills catalog: incident-triage at <span class=\"mono\">{MAIN[:7]}</span>" in page
    assert "Egress on 443" not in page   # skills reach nothing


@pytest.mark.parametrize("name, environment, message", [
    ("release-notes", "prod", "not approved for prod"), ("finance-close", "prod", "for some groups only"),
    ("finance-close", "test", "not approved for test"), ("nope", "test", "not in the catalog"), ("", "test", "Choose a skill from the list."),
    ("../x", "test", "Choose a skill from the list.")])
def test_crafted_posts_are_refused_and_nothing_is_stored(skills, name, environment, message):
    response = add(skills, name, environment=environment)
    assert response.status_code == 400 and message in html.unescape(response.text) and not bindings(skills)


def test_a_person_in_the_audience_may_add_finance_close_in_prod(skills):
    in_groups(skills, ["finance-analysts"])
    assert add(skills, "finance-close", environment="prod").status_code == 303
    assert bindings(skills)[0].final["skills"][0]["name"] == "finance-close"


def test_a_duplicate_and_a_21st_skill_are_refused(portal, tmp_path):
    doc = json.loads(skills_catalog.MOCK.read_text())
    main = doc["commits"][doc["main"]]["skills"]
    for i in range(20):
        main[f"s{i:02d}"] = {"skill.yaml": f"schema: skill/v1\nname: s{i:02d}\ndescription: A skill.\nenvironments: [test]\naudience: everyone\n",
                             "SKILL.md": "# S\n"}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc))
    with_catalog(portal, skills_catalog.load_mock(path))
    add_and_merge(portal)
    response = add(portal)
    assert response.status_code == 400 and "use Update" in response.text and len(bindings(portal)) == 1
    for i in range(19):
        add_and_merge(portal, f"s{i:02d}")
    assert len(bindings(portal)[0].final["skills"]) == 20
    response = add(portal, "s19")
    assert response.status_code == 400 and "at most 20 skills" in response.text and len(bindings(portal)) == 20


def test_update_re_pins_an_old_skill_to_main(skills):
    pinned_old_and_merged(skills)
    page = skills.client("owner").get("/apps/acme/notes").text
    assert f'<td class="mono">{OLD[:7]}</td><td>newer: <span class="mono">{MAIN[:7]}</span></td>' in page
    assert 'action="/apps/acme/notes/skills/update"' in page and '<input type="hidden" name="name" value="incident-triage">' in page
    response = post(skills, "owner", "/apps/acme/notes/skills/update", name="incident-triage")
    assert response.status_code == 303 and any(c.startswith(f"{FLASH_COOKIE}=skill") for c in response.headers.get_list("set-cookie"))
    newest = bindings(skills)[0]
    assert newest.state == "approved" and newest.final["skills"] == [{"name": "incident-triage", "commit": MAIN, "verified_by": "mock-skills-catalog"}]
    assert len(bindings(skills)) == 2   # a new request, like any change


def test_update_on_a_skill_at_main_or_not_bound_is_a_400(skills):
    add_and_merge(skills)
    response = post(skills, "owner", "/apps/acme/notes/skills/update", name="incident-triage")
    assert response.status_code == 400 and "already at the catalog&#39;s current version" in response.text and len(bindings(skills)) == 1
    response = post(skills, "owner", "/apps/acme/notes/skills/update", name="release-notes")
    assert response.status_code == 400 and "nothing to update" in response.text and len(bindings(skills)) == 1


def test_removing_a_skill_asks_first(skills):
    add_and_merge(skills)
    page = skills.client("owner").get("/apps/acme/notes").text
    assert 'href="/apps/acme/notes?environment=test&amp;confirm_skill=incident-triage"' in page and 'action="/apps/acme/notes/skills/remove"' not in page
    ask = skills.client("owner").get("/apps/acme/notes", params={"confirm_skill": "incident-triage"})
    assert ask.status_code == 200 and "Remove the skill incident-triage?" in ask.text
    assert "The app no longer mounts /skills/incident-triage in this environment. The skill stays in the catalog." in ask.text
    assert 'action="/apps/acme/notes/skills/remove"' in ask.text and 'name="name" value="incident-triage"' in ask.text
    assert skills.client("owner").get("/apps/acme/notes", params={"confirm_skill": "release-notes"}).status_code == 400
    refused = post(skills, "owner", "/apps/acme/notes/skills/remove", name="incident-triage")
    assert refused.status_code == 400 and "Confirm the removal first" in refused.text and len(bindings(skills)) == 1
    response = post(skills, "owner", "/apps/acme/notes/skills/remove", name="incident-triage", confirmed="yes")
    assert response.status_code == 303 and bindings(skills)[0].final["skills"] == []


def test_strangers_get_404_and_portal_admins_403(skills):
    for path in ("skills", "skills/update", "skills/remove"):
        assert post(skills, "other", f"/apps/acme/notes/{path}", name="incident-triage", confirmed="yes").status_code == 404
        assert post(skills, "admin", f"/apps/acme/notes/{path}", name="incident-triage", confirmed="yes").status_code == 403
    assert get(skills, who="other").status_code == 404 and get(skills, who="admin", environment="test").status_code == 403
    assert not bindings(skills) and "/skills/new" not in skills.client("admin").get("/apps/acme/notes").text


def test_the_posts_need_csrf(skills):
    for path in ("skills", "skills/update", "skills/remove"):
        assert skills.client("owner").post(f"/apps/acme/notes/{path}", data={"name": "incident-triage"}).status_code == 403
    assert not bindings(skills)


def test_prod_and_test_are_separate(skills):
    assert add(skills, environment="prod").status_code == 303
    assert get(skills, environment="prod").status_code == 409 and get(skills, environment="test").status_code == 200
    assert add(skills, environment="test").status_code == 303
    assert {r.final["environment"] for r in bindings(skills)} == {"test", "prod"}


def test_catalog_text_is_escaped(portal, tmp_path):
    doc = json.loads(skills_catalog.MOCK.read_text())
    entry = doc["commits"][doc["main"]]["skills"]["release-notes"]
    entry["skill.yaml"] = entry["skill.yaml"].replace("Draft release notes from merged pull request titles.", "'<script>alert(1)</script>'")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc))
    with_catalog(portal, skills_catalog.load_mock(path))
    text = get(portal, environment="test").text
    assert "<script>alert(1)</script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_skills_work_without_the_directory(portal):
    portal.app.state.settings = make_settings(skills_catalog=skills_catalog.load_mock())
    page = portal.client("owner").get("/apps/acme/notes").text
    assert "Skills (platform catalog)" in page and "Associate Azure Identity" not in page
    add_and_merge(portal)
    assert post(portal, "owner", "/apps/acme/notes/skills/remove", name="incident-triage", confirmed="yes").status_code == 303


def test_the_steps_pages_are_free_of_inline_style_and_script(skills):
    from test_portal_csp_pages import problems
    for params in ({}, {"environment": "test"}, {"environment": "test", "name": "incident-triage"}, {"environment": "prod", "name": "release-notes"}):
        assert problems(get(skills, **params).text) == [], params


def test_a_skill_that_left_the_catalog_says_so_and_offers_no_update(skills, tmp_path):
    pinned_old_and_merged(skills)
    doc = json.loads(skills_catalog.MOCK.read_text())
    del doc["commits"][doc["main"]]["skills"]["incident-triage"]
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(doc))
    with_catalog(skills, skills_catalog.load_mock(path))
    page = skills.client("owner").get("/apps/acme/notes").text
    assert "<td>no longer in the catalog</td>" in page and "/skills/update" not in page and "newer:" not in page
    assert "confirm_skill=incident-triage" in page   # it can still be removed
