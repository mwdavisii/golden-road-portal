"""The portal's skills catalog (spec 2026-10-31 Part 2): the same fixture deploy-verify reads, a skill at a pinned commit, what an
environment and a person's groups are offered at main, and a strict loader."""

import json

import pytest

from portal import compose
from portal.skills_catalog import MOCK, Skill, SkillsCatalogError, load_mock

DOC = json.loads(MOCK.read_text(encoding="utf-8"))
OLD, MAIN = "2c7d1e9f0a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d", "6b1f0c2d9e8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"


def names(skills):
    return [s.name for s in skills]


def test_the_shipped_catalog_loads():
    cat = load_mock()
    assert (cat.name, cat.repo, cat.main) == ("mock-skills-catalog", "example-org/golden-road-skills", MAIN)


def test_what_each_environment_and_audience_is_offered_at_main():
    cat = load_mock()
    assert names(cat.offered("test", groups=())) == ["incident-triage", "release-notes"]
    assert names(cat.offered("prod", ())) == ["incident-triage"]
    assert names(cat.offered("prod", ("finance-analysts",))) == ["finance-close", "incident-triage"]
    assert names(cat.offered("test", ("finance-analysts",))) == ["incident-triage", "release-notes"]  # finance-close is prod only
    assert names(cat.offered("prod", ("Finance-Analysts",))) == ["incident-triage"]  # exact groups, as roles are matched
    assert names(cat.offered("stage", ())) == []


def test_a_skill_at_a_commit_has_that_commits_files_without_skill_yaml():
    cat = load_mock()
    old = cat.skill("incident-triage", OLD)
    assert old.files == {"SKILL.md": DOC["commits"][OLD]["skills"]["incident-triage"]["SKILL.md"]}
    now = cat.skill("incident-triage", MAIN)
    assert sorted(now.files) == ["SKILL.md", "checklists/sev1.md"]
    assert (now.description, now.environments, now.audience) == (
        "Triage an incident report into severity, owner and next step.", ("test", "prod"), "everyone")
    assert cat.skill("finance-close", MAIN).audience == ("finance-analysts",)


@pytest.mark.parametrize("name, commit", [("release-notes", OLD), ("nope", MAIN), ("incident-triage", "f" * 40),
                                          ("../incident-triage", MAIN), ("incident-triage", "HEAD"), (None, MAIN), ("incident-triage", None)])
def test_a_skill_not_at_that_commit_is_none(name, commit):
    assert load_mock().skill(name, commit) is None


def test_each_read_is_a_fresh_copy():
    cat = load_mock()
    cat.skill("incident-triage", MAIN).files["SKILL.md"] = "changed"
    assert cat.skill("incident-triage", MAIN).files["SKILL.md"] != "changed"
    cat.offered("test", ())[0].files["SKILL.md"] = "changed"
    assert cat.offered("test", ())[0].files["SKILL.md"] != "changed"


def test_every_skill_at_every_commit_is_within_composes_limits():
    cat = load_mock()
    for commit, entry in DOC["commits"].items():
        for name in entry["skills"]:
            assert compose.skill_files_problem(name, cat.skill(name, commit).files) is None, (name, commit)


@pytest.mark.parametrize("environments, audience, environment, groups, allowed", [
    (("test", "prod"), "everyone", "test", (), True), (("test",), "everyone", "prod", (), False),
    (("prod",), ("finance-analysts",), "prod", ("finance-analysts",), True), (("prod",), ("finance-analysts",), "prod", (), False),
    (("prod",), ("finance-analysts",), "prod", ["other", "finance-analysts"], True), (("prod",), ("a", "b"), "prod", ("c",), False),
    (("prod",), ("finance-analysts",), "test", ("finance-analysts",), False), (("test",), ("everyone",), "test", (), False),
])
def test_allows_is_the_one_rule(environments, audience, environment, groups, allowed):
    assert Skill("s", "d", environments, audience, {}).allows(environment, groups) is allowed


def write(tmp_path, doc):
    (tmp_path / "c.json").write_text(json.dumps(doc))
    return tmp_path / "c.json"


def skills_at(d, commit=MAIN):
    return d["commits"][commit]["skills"]


@pytest.mark.parametrize("edit", [
    lambda d: d.update(schema="x"), lambda d: d.update(main="abc"), lambda d: d.update(main="e" * 40), lambda d: d.update(extra=1),
    lambda d: d.update(repo="not a repo"), lambda d: d.pop("repo"), lambda d: d["commits"].update({"HEAD": {"skills": {}}}),
    lambda d: d["commits"][MAIN].update(extra=1), lambda d: d["commits"].update({"a" * 40: []}),
    lambda d: skills_at(d)["release-notes"].update({"skill.yaml": "schema: skill/v1\nname: other\n"}),
    lambda d: skills_at(d)["release-notes"].update({"skill.yaml": skills_at(d)["release-notes"]["skill.yaml"].replace(
        "name: release-notes", "name: incident-triage")}),
    lambda d: skills_at(d)["release-notes"].update({"skill.yaml": "schema: skill/v1\nschema: skill/v1\n"}),
    lambda d: skills_at(d)["release-notes"].pop("skill.yaml"), lambda d: skills_at(d)["release-notes"].pop("SKILL.md"),
    lambda d: skills_at(d)["release-notes"].update({"skill.yaml": "[unclosed"}),
    lambda d: skills_at(d).update({"Bad_Name": skills_at(d)["release-notes"]}),
    lambda d: skills_at(d)["release-notes"].update({"SKILL.md": 7}),
    lambda d: skills_at(d)["release-notes"].update({"../escape.md": "x"}),
    lambda d: skills_at(d)["release-notes"].update({"a__b.md": "x"}),
    lambda d: skills_at(d)["release-notes"].update({".hidden": "x"}),
    lambda d: skills_at(d)["release-notes"].update({"big.md": "x" * (compose.SKILL_MAX_BYTES + 1)}),
    lambda d: skills_at(d)["release-notes"].update({f"f{n}.md": "x" for n in range(compose.SKILL_MAX_FILES)}),
])
def test_a_bad_catalog_is_refused(tmp_path, edit):
    doc = json.loads(json.dumps(DOC))
    edit(doc)
    with pytest.raises(SkillsCatalogError):
        load_mock(write(tmp_path, doc))


def test_a_catalog_naming_a_key_twice_or_not_loading_is_refused(tmp_path):
    (tmp_path / "c.json").write_text(MOCK.read_text().replace('"schema": "mock-skills-catalog/v1",',
                                                              '"schema": "mock-skills-catalog/v1", "schema": "mock-skills-catalog/v1",'))
    with pytest.raises(SkillsCatalogError, match="twice"):
        load_mock(tmp_path / "c.json")
    with pytest.raises(SkillsCatalogError, match="can't be read"):
        load_mock(tmp_path / "missing.json")


def test_deeply_nested_json_is_refused(tmp_path):
    (tmp_path / "c.json").write_text("[" * 100000 + "]" * 100000)
    with pytest.raises(SkillsCatalogError, match="can't be read"):
        load_mock(tmp_path / "c.json")


@pytest.mark.parametrize("groups", ["finance-analysts", "finance-analysts,other"])
def test_groups_given_as_a_string_never_match(groups):
    assert Skill("s", "d", ("prod",), ("finance-analysts", "f", "i"), {}).allows("prod", groups) is False
    assert names(load_mock().offered("prod", groups)) == ["incident-triage"]
