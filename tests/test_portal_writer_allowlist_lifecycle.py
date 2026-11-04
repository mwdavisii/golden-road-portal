import pytest

from portal.writer.allowlist import LIFECYCLE_SCHEMA, adapter_name, allowed, lifecycle_merges, lifecycle_path_allowed

DEPLOY, FACTORY = "acme/golden-road-deploy", "acme/scorecard-factory"


def test_lifecycle_yaml_is_a_deploy_file_with_its_schema():
    assert allowed("acme", DEPLOY, "apps/acme/notes/prod/lifecycle.yaml") == LIFECYCLE_SCHEMA == "lifecycle.v1"
    assert allowed("acme", DEPLOY, "apps/acme/../prod/lifecycle.yaml") is None
    assert allowed("acme", FACTORY, "apps/acme/notes/prod/lifecycle.yaml") is None


@pytest.mark.parametrize("path, ok", [
    ("apps/acme/notes/test/lifecycle.yaml", True), ("apps/acme/notes/prod/composed.yaml", True),
    ("apps/acme/notes/prod/bindings.yaml", False), ("apps/acme/notes/prod/release.yaml", False),
    ("apps/acme/other/prod/lifecycle.yaml", False), ("clusters/home/flux/acme-notes-prod.yaml", False)])
def test_a_suspend_or_resume_writes_only_its_app_s_lifecycle_and_composed(path, ok):
    for action in ("suspend", "resume"):
        assert lifecycle_path_allowed("acme", DEPLOY, path, "acme/notes", action, "deploy") is ok


@pytest.mark.parametrize("path, ok", [
    ("apps/acme/notes/test/release.yaml", True), ("apps/acme/notes/prod/base/manifests.yaml", True),
    ("apps/acme/notes/prod/cloudflare-access.yaml", True), ("apps/acme/notes/prod/lifecycle.yaml", True),
    ("clusters/home/flux/acme-notes-prod.yaml", True), ("clusters/office/argo/acme-notes-test.yaml", True),
    ("clusters/home/flux/acme-other-prod.yaml", False), ("clusters/home/flux/gitrepository.yaml", False),
    ("apps/acme/other/test/release.yaml", False), ("apps/acme/notes/test/extra.yaml", False), ("policy/cluster-image-policy.yaml", False)])
def test_a_decommission_deletes_only_its_app_and_its_adapters(path, ok):
    assert lifecycle_path_allowed("acme", DEPLOY, path, "acme/notes", "decommission", "deploy") is ok
    assert lifecycle_path_allowed("acme", DEPLOY, path, "acme/notes", "suspend", "deploy") is (ok and path.endswith(("lifecycle.yaml", "composed.yaml")) and path.startswith("apps/"))


def test_the_factory_step_writes_only_the_app_s_intake_record():
    assert lifecycle_path_allowed("acme", FACTORY, "intake/acme/notes.yaml", "acme/notes", "decommission", "factory")
    assert not lifecycle_path_allowed("acme", FACTORY, "intake/acme/other.yaml", "acme/notes", "decommission", "factory")
    assert not lifecycle_path_allowed("acme", FACTORY, "intake/acme/notes.yaml", "acme/notes", "suspend", "factory")
    assert not lifecycle_path_allowed("acme", DEPLOY, "intake/acme/notes.yaml", "acme/notes", "decommission", "factory")


def test_the_archive_step_writes_nothing_and_another_org_s_repo_is_never_a_target():
    assert not lifecycle_path_allowed("acme", DEPLOY, "apps/acme/notes/test/lifecycle.yaml", "acme/notes", "suspend", "archive")
    assert not lifecycle_path_allowed("acme", "other/golden-road-deploy", "apps/acme/notes/test/lifecycle.yaml", "acme/notes", "suspend", "deploy")
    assert not lifecycle_path_allowed("acme", DEPLOY, "apps/acme/notes/test/lifecycle.yaml", "acme/notes", "archive", "deploy")


def test_only_a_suspend_or_resume_deploy_step_is_writer_merged():
    assert lifecycle_merges("suspend", "deploy") and lifecycle_merges("resume", "deploy")
    assert not lifecycle_merges("decommission", "deploy") and not lifecycle_merges("decommission", "factory")


def test_the_adapter_name_is_deploy_s():
    assert adapter_name("acme", "notes", "prod") == "acme-notes-prod"
    assert adapter_name("Acme", "my_app.v2", "test") == "acme-my-app-v2-test"
