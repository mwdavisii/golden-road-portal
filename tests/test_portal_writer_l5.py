"""L5 (spec 2026-11-02): the proof the writer runs on a suspend or resume PR, as GitHub shows it, before it merges it itself."""

import yaml
from portal_fakes import RepoWorld, deploy_world

from portal.writer.lifecycle import l5_problems

REQ = type("Req", (), {"id": "r_" + "d" * 32, "repo": "acme/notes", "final": {"task": "SCTASK0010001"}, "requested_by": "p_" + "a" * 32})()


def world_files(env_lifecycle=None):
    world = RepoWorld()
    deploy, _ = deploy_world(world, lifecycle=env_lifecycle)
    return world.files(deploy)


def suspended(env, request=REQ.id):
    return yaml.safe_dump({"schema": "lifecycle/v1", "repo": "acme/notes", "environment": env, "state": "suspended", "task": "SCTASK0010001",
                           "reason": "Critical vulnerability CVE-2026-0001; suspended until patched", "requested_by": "p_" + "a" * 32,
                           "requested_at": "2026-11-02T22:00:00Z", "request": request}, sort_keys=False).encode()


def reader(base, head):
    return lambda path, which: (base if which == "base" else head).get(path)


def suspend_pr(envs=("test", "prod")):
    base = world_files()
    head = world_files({e: suspended(e) for e in envs})
    files = [{"filename": f"apps/acme/notes/{e}/{n}", "status": s} for e in envs for n, s in (("lifecycle.yaml", "added"), ("composed.yaml", "modified"))]
    return files, base, head


def test_a_true_suspend_passes_and_so_does_a_true_resume():
    files, base, head = suspend_pr()
    assert l5_problems("suspend", REQ, files, reader(base, head)) == []
    back = [{"filename": f["filename"], "status": "removed" if f["filename"].endswith("lifecycle.yaml") else "modified"} for f in files]
    assert l5_problems("resume", REQ, back, reader(head, base)) == []


def test_a_pr_that_adds_a_binding_is_refused():
    files, base, head = suspend_pr()
    head["apps/acme/notes/prod/bindings.yaml"] = b"schema: binding/v2\n"
    problems = l5_problems("suspend", REQ, files + [{"filename": "apps/acme/notes/prod/bindings.yaml", "status": "added"}], reader(base, head))
    assert problems == ["apps/acme/notes/prod/bindings.yaml (added) is not a suspend's file"]


def test_a_composed_yaml_that_changes_the_image_is_refused():
    files, base, head = suspend_pr()
    text = head["apps/acme/notes/prod/composed.yaml"].decode()
    head["apps/acme/notes/prod/composed.yaml"] = text.replace("notes@sha256:" + "b" * 64, "notes@sha256:" + "c" * 64).encode()
    assert l5_problems("suspend", REQ, files, reader(base, head)) == [
        "apps/acme/notes/prod/composed.yaml changes more than the Deployment's replicas"]


def test_a_pr_that_touches_another_environment_without_its_lifecycle_file_is_refused():
    files, base, head = suspend_pr(("test",))
    other = world_files({"prod": suspended("prod")})
    head["apps/acme/notes/prod/composed.yaml"] = other["apps/acme/notes/prod/composed.yaml"]
    files += [{"filename": "apps/acme/notes/prod/composed.yaml", "status": "modified"}]
    assert l5_problems("suspend", REQ, files, reader(base, head)) == [
        "apps/acme/notes/prod: a suspend changes both lifecycle.yaml and composed.yaml"]


def test_a_pr_that_touches_another_app_is_refused():
    files, base, head = suspend_pr(("test",))
    files += [{"filename": "apps/acme/other/test/composed.yaml", "status": "modified"}]
    assert l5_problems("suspend", REQ, files, reader(base, head)) == ["apps/acme/other/test/composed.yaml (modified) is not a suspend's file"]


def test_a_lifecycle_file_with_the_wrong_status_is_refused():
    files, base, head = suspend_pr(("test",))
    files[0] = {**files[0], "status": "modified"}
    assert l5_problems("suspend", REQ, files, reader(base, head)) == ["apps/acme/notes/test/lifecycle.yaml (modified) is not a suspend's file"]


def test_replicas_one_while_claiming_suspend_is_refused():
    files, base, head = suspend_pr(("prod",))
    head["apps/acme/notes/prod/composed.yaml"] = base["apps/acme/notes/prod/composed.yaml"] + b"\n"  # the same objects: replicas stays 1
    assert l5_problems("suspend", REQ, files, reader(base, head)) == ["apps/acme/notes/prod/composed.yaml sets replicas to 1, not 0"]


def test_a_resume_must_go_back_to_the_base_manifests_value():
    files, base, head = suspend_pr(("prod",))
    back = [{"filename": "apps/acme/notes/prod/lifecycle.yaml", "status": "removed"}, {"filename": "apps/acme/notes/prod/composed.yaml", "status": "modified"}]
    wrong = dict(base)
    wrong["apps/acme/notes/prod/composed.yaml"] = base["apps/acme/notes/prod/composed.yaml"].replace(b"replicas: 1", b"replicas: 3")
    assert l5_problems("resume", REQ, back, reader(head, wrong)) == ["apps/acme/notes/prod/composed.yaml sets replicas to 3, not 1"]


def test_a_lifecycle_file_that_is_not_this_request_s_is_refused():
    files, base, head = suspend_pr(("test",))
    head["apps/acme/notes/test/lifecycle.yaml"] = suspended("test", request="r_" + "e" * 32)
    assert l5_problems("suspend", REQ, files, reader(base, head)) == ["apps/acme/notes/test/lifecycle.yaml is not this request's suspension"]


def test_a_composed_yaml_that_can_t_be_read_is_refused():
    files, base, head = suspend_pr(("test",))
    head["apps/acme/notes/test/composed.yaml"] = b"\xff\xfe not yaml"
    assert l5_problems("suspend", REQ, files, reader(base, head)) == ["apps/acme/notes/test/composed.yaml can't be compared"]


def test_an_empty_pr_is_refused():
    assert l5_problems("suspend", REQ, [], reader({}, {})) == ["the pull request changes no environment"]


# ---- replicas absent from base/manifests.yaml (compose leaves it absent on resume) ----

def no_replicas_files(env_lifecycle=None):
    from portal_fakes import FIXTURES
    manifests = (FIXTURES / "deploy/manifests.yaml").read_bytes().replace(b"  replicas: 1\n", b"")
    assert b"replicas" not in manifests
    world = RepoWorld()
    deploy, _ = deploy_world(world, lifecycle=env_lifecycle, manifests=manifests)
    return world.files(deploy)


def resume_files(env="prod"):
    return [{"filename": f"apps/acme/notes/{env}/lifecycle.yaml", "status": "removed"},
            {"filename": f"apps/acme/notes/{env}/composed.yaml", "status": "modified"}]


def test_a_resume_with_no_replicas_in_the_manifests_passes_with_replicas_absent():
    susp, run = no_replicas_files({"prod": suspended("prod")}), no_replicas_files()
    assert b"replicas" not in run["apps/acme/notes/prod/composed.yaml"]
    assert l5_problems("resume", REQ, resume_files(), reader(susp, run)) == []


def test_a_suspend_with_no_replicas_in_the_manifests_still_sets_zero():
    run, susp = no_replicas_files(), no_replicas_files({"prod": suspended("prod")})
    files = [{"filename": "apps/acme/notes/prod/lifecycle.yaml", "status": "added"},
             {"filename": "apps/acme/notes/prod/composed.yaml", "status": "modified"}]
    assert l5_problems("suspend", REQ, files, reader(run, susp)) == []


def test_a_resume_that_sets_replicas_the_manifests_leave_out_is_refused():
    susp, run = no_replicas_files({"prod": suspended("prod")}), no_replicas_files()
    head = dict(run)
    head["apps/acme/notes/prod/composed.yaml"] = susp["apps/acme/notes/prod/composed.yaml"].replace(b"replicas: 0", b"replicas: 1")
    assert l5_problems("resume", REQ, resume_files(), reader(susp, head)) == [
        "apps/acme/notes/prod/composed.yaml sets replicas to 1, not no replicas"]


def test_a_resume_that_drops_replicas_the_manifests_set_is_refused():
    files, base, head = suspend_pr(("prod",))
    wrong = dict(base)
    wrong["apps/acme/notes/prod/composed.yaml"] = base["apps/acme/notes/prod/composed.yaml"].replace(b"  replicas: 1\n", b"")
    assert l5_problems("resume", REQ, resume_files(), reader(head, wrong)) == [
        "apps/acme/notes/prod/composed.yaml sets replicas to no replicas, not 1"]


def test_a_suspend_that_drops_replicas_or_sets_a_non_integer_zero_is_refused():
    files, base, head = suspend_pr(("prod",))
    for wrong, shown in ((b"", "no replicas"), (b"  replicas: '0'\n", "'0'"), (b"  replicas: false\n", "False"), (b"  replicas: 0.0\n", "0.0")):
        bad = dict(head)
        bad["apps/acme/notes/prod/composed.yaml"] = head["apps/acme/notes/prod/composed.yaml"].replace(b"  replicas: 0\n", wrong)
        assert l5_problems("suspend", REQ, files, reader(base, bad)) == [
            f"apps/acme/notes/prod/composed.yaml sets replicas to {shown}, not 0"], wrong


# ---- what else a composed.yaml may not change ----

def _edit(head, old, new, env="prod"):
    path = f"apps/acme/notes/{env}/composed.yaml"
    text = head[path]
    assert old in text, old
    return {**head, path: text.replace(old, new, 1)}


MORE = "apps/acme/notes/prod/composed.yaml changes more than the Deployment's replicas"


def test_a_changed_service_is_refused():
    files, base, head = suspend_pr(("prod",))
    bad = _edit(head, b"    port: 80\n", b"    port: 81\n")
    assert l5_problems("suspend", REQ, files, reader(base, bad)) == [MORE]


def test_a_changed_configmap_is_refused():
    from portal.writer.run import compose_bytes
    from test_portal_writer_lifecycle import BINDING_TEST
    world = RepoWorld()
    deploy, _ = deploy_world(world, bindings={"test": BINDING_TEST})
    base = world.files(deploy)
    d = "apps/acme/notes/test"
    head = {**base, f"{d}/lifecycle.yaml": suspended("test")}
    head[f"{d}/composed.yaml"] = compose_bytes("acme/notes", "test", base[f"{d}/release.yaml"], base[f"{d}/base/manifests.yaml"], BINDING_TEST,
                                               None, suspended("test"))
    files = [{"filename": f"{d}/lifecycle.yaml", "status": "added"}, {"filename": f"{d}/composed.yaml", "status": "modified"}]
    assert l5_problems("suspend", REQ, files, reader(base, head)) == []
    # a ConfigMap the same composed.yaml carries, changed
    extra = b"---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes-extra\ndata:\n  a: '1'\n"
    base2 = {**base, f"{d}/composed.yaml": base[f"{d}/composed.yaml"] + extra}
    head2 = {**head, f"{d}/composed.yaml": head[f"{d}/composed.yaml"] + extra.replace(b"a: '1'", b"a: '2'")}
    assert l5_problems("suspend", REQ, files, reader(base2, head2)) == [f"{d}/composed.yaml changes more than the Deployment's replicas"]


def test_replicas_changed_on_another_object_is_refused():
    files, base, head = suspend_pr(("prod",))
    base2 = _edit(base, b"  type: ClusterIP\n", b"  type: ClusterIP\n  replicas: 1\n")
    head2 = _edit(head, b"  type: ClusterIP\n", b"  type: ClusterIP\n  replicas: 0\n")
    assert l5_problems("suspend", REQ, files, reader(base2, head2)) == [MORE]


def test_a_duplicate_key_is_refused():
    files, base, head = suspend_pr(("prod",))
    bad = _edit(head, b"  replicas: 0\n", b"  replicas: 0\n  replicas: 5\n")
    assert l5_problems("suspend", REQ, files, reader(base, bad)) == ["apps/acme/notes/prod/composed.yaml can't be compared"]


def test_an_extra_yaml_document_is_refused():
    files, base, head = suspend_pr(("prod",))
    path = "apps/acme/notes/prod/composed.yaml"
    for extra in (b"---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n",
                  b"---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: x\nspec:\n  replicas: 0\n"):
        bad = {**head, path: head[path] + extra}
        assert l5_problems("suspend", REQ, files, reader(base, bad)) in ([MORE], [f"{path} can't be compared"]), extra
