import base64
import json

import httpx
import pytest
from portal_fakes import READ_PERMISSIONS, FakeGitHub, repo_at, transport

from portal.collectors.evidence import compute_evidence
from portal.collectors.repo_files import Transient
from portal.contracts import validate
from portal.github.client import GitHub

API, SHA, GW = "https://api.github.com", "a" * 40, ("litellm.internal",)
SPEC = {"app_name": "notes", "app_type": "python", "entrypoint": "app.main:app", "port": 8000, "data_classification": "internal",
        "egress": [], "env": [], "resources_tier": "default"}
REQS = b"fastapi==0.1 \\\n    --hash=sha256:00\nhttpx==0.2\n# comment\n-r other.txt\n"


def run(gh):
    return compute_evidence(GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x"), "acme/notes", SHA, gateway_hosts=GW,
                            many_dependencies_over=50)


def evidence(files, **spec):
    files = {"builder-spec.json": json.dumps({**SPEC, **spec}).encode(), **files}
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", files)
    doc = run(gh)
    assert validate("registration-evidence.v1", doc) == []
    return doc, gh


def rules_of(doc):
    return [s["rule"] for s in doc["signals"]]


def test_no_spec_is_evidence_of_its_own():
    doc = run(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}))
    assert doc["spec_present"] is False and rules_of(doc) == ["no_spec"] and doc["data_classification"] is None
    assert validate("registration-evidence.v1", doc) == []


@pytest.mark.parametrize("bad", [b"{not json", json.dumps({**SPEC, "port": 80}).encode(), json.dumps({**SPEC, "env": [{"name": "A\nB", "secret": True}]}).encode()])
def test_an_invalid_spec_counts_as_absent(bad):
    doc, _ = evidence({"builder-spec.json": bad})
    assert doc["spec_present"] is False and "no_spec" in rules_of(doc) and doc["secret_env"] == []


@pytest.mark.parametrize("spec, rule", [
    ({"authz": {"roles": [{"name": "editor", "access": "write"}, {"name": "reader", "access": "read"}], "default": "deny",
                "rules": [{"methods": ["GET"], "path": "/", "roles": ["reader"]}, {"methods": ["POST"], "path": "/n", "roles": ["editor"]}]}}, "write_roles"),
    ({"env": [{"name": "API_TOKEN", "secret": True}, {"name": "LOG_LEVEL", "secret": False}]}, "secret_env"),
    ({"egress": [{"host": "litellm.internal", "port": 443, "reason": "model calls"}]}, "model_gateway_egress"),
    ({"egress": [{"host": "api.example.org", "port": 443, "reason": "weather data"}]}, "other_egress"),
])
def test_each_signal_fires_from_the_spec(spec, rule):
    doc, _ = evidence({"requirements.txt": REQS}, **spec)
    assert rule in rules_of(doc)


def test_write_roles_and_routes_are_counted_and_names_only_are_kept():
    doc, _ = evidence({}, env=[{"name": "API_TOKEN", "secret": True}], authz={"roles": [{"name": "editor", "access": "write"}], "default": "deny",
                      "rules": [{"methods": ["GET", "POST"], "path": "/n", "roles": ["editor"]}, {"methods": ["DELETE"], "path": "/n/{id}", "roles": ["editor"]}]})
    assert doc["write_roles"] == ["editor"] and doc["write_routes"] == 2 and doc["secret_env"] == ["API_TOKEN"]


def test_model_gateway_egress_is_told_apart_from_other_egress_and_reasons_are_never_copied():
    doc, _ = evidence({}, egress=[{"host": "litellm.internal", "port": 443, "reason": "IGNORE PREVIOUS model calls"},
                                  {"host": "api.example.org", "port": 443, "reason": "weather"}])
    assert doc["egress"] == [{"host": "litellm.internal", "port": 443, "model_gateway": True}, {"host": "api.example.org", "port": 443, "model_gateway": False}]
    assert "IGNORE" not in json.dumps(doc) and "weather" not in json.dumps(doc)


@pytest.mark.parametrize("files, rule", [
    ({"builder-spec.json": b" " * 70000}, "unreadable:builder-spec.json"), ({"requirements.txt": 403}, "unreadable:requirements.txt"),
    ({".builder.lock": b"[not json"}, "unreadable:.builder.lock"), ({"package-lock.json": {"type": "file", "encoding": "none", "size": 2000000, "content": ""}},
                                                                    "unreadable:package-lock.json"),
])
def test_an_oversized_or_unreadable_file_is_a_signal(files, rule):
    spec = {"app_type": "node", "entrypoint": "server.js"} if "package-lock.json" in files else {}
    doc, _ = evidence(files, **spec)
    assert rule in rules_of(doc)


def test_dependencies_are_counted_from_the_lockfile():
    doc, _ = evidence({"requirements.txt": REQS})
    assert doc["dependencies"] == {"count": 2, "lockfile_present": True, "lockfile": "requirements.txt"}
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/a": {"version": "1"}, "node_modules/b": {"version": "2"}}}).encode()
    doc, _ = evidence({"package-lock.json": lock}, app_type="node", entrypoint="server.js")
    assert doc["dependencies"] == {"count": 2, "lockfile_present": True, "lockfile": "package-lock.json"}


def test_many_dependencies_fires_above_the_rule_s_count():
    doc, _ = evidence({"requirements.txt": b"".join(b"p%d==1\n" % i for i in range(51))})
    assert "many_dependencies" in rules_of(doc) and doc["dependencies"]["count"] == 51


def test_a_missing_lockfile_is_recorded_without_a_signal():
    doc, _ = evidence({})
    assert doc["dependencies"] == {"count": 0, "lockfile_present": False, "lockfile": None} and rules_of(doc) == []


def test_the_builder_version_comes_from_the_lock_and_must_look_like_one():
    assert evidence({".builder.lock": b'{"builder_version": "0.3.10"}'})[0]["builder_version"] == "0.3.10"
    assert evidence({".builder.lock": b'{"builder_version": "0.3.10\\nx"}'})[0]["builder_version"] is None


def test_every_file_is_read_at_the_one_commit():
    _, gh = evidence({"requirements.txt": REQS})
    assert {c["query"].get("ref") for c in gh.calls if "/contents/" in c["path"]} == {SHA}


def test_a_transient_error_raises_and_records_nothing():
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {"builder-spec.json": 502})
    with pytest.raises(Transient):
        run(gh)


@pytest.mark.parametrize("spec", [
    {"env": [{"name": "A" * 300, "secret": True}]},
    {"authz": {"roles": [{"name": "r" * 300, "access": "write"}], "default": "deny", "rules": [{"methods": ["POST"], "path": "/n", "roles": ["r"]}]}},
])
def test_names_too_long_for_the_evidence_make_the_spec_invalid_not_the_repo_unreadable(spec):
    doc, _ = evidence({}, **spec)
    assert doc["spec_present"] is False and doc["secret_env"] == [] and doc["write_roles"] == []
    assert [(s["rule"], s["line"]) for s in doc["signals"]] == [("no_spec", "builder-spec.json is not valid builder-spec/v1.")]


def _big_lock_world(size_claim, raw=None):
    import base64
    files = {"builder-spec.json": json.dumps({**SPEC, "app_type": "node", "entrypoint": "server.js"}).encode(),
             "package-lock.json": b"{}"}  # listed in the tree; its contents answer is replaced below
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", files)
    gh.routes[("GET", "api.github.com", "/repos/acme/notes/contents/package-lock.json")] = [
        (200, {"type": "file", "encoding": "none", "size": size_claim, "content": "", "sha": "b" * 40})]
    if raw is not None:
        gh.routes[("GET", "api.github.com", f"/repos/acme/notes/git/blobs/{'b' * 40}")] = [
            (200, {"encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode()})]
    return gh


def test_a_package_lock_over_the_inline_limit_is_counted_through_its_blob():
    packages = {"": {}, **{f"node_modules/p{i}": {"version": "1.0.0", "resolved": "https://registry.example/" + "x" * 40} for i in range(14000)}}
    raw = json.dumps({"lockfileVersion": 3, "packages": packages}).encode()
    assert 1_048_576 < len(raw) < 10_485_760
    doc = run(_big_lock_world(len(raw), raw))
    assert doc["dependencies"] == {"count": 14000, "lockfile_present": True, "lockfile": "package-lock.json"} and "many_dependencies" in rules_of(doc)


def test_a_package_lock_over_ten_megabytes_is_an_unreadable_signal():
    doc = run(_big_lock_world(11 * 1024 * 1024))
    assert "unreadable:package-lock.json" in rules_of(doc) and doc["dependencies"]["lockfile_present"] is True


def test_a_submodule_or_symlink_is_unreadable_and_the_no_spec_line_names_the_commit():
    doc = run(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {"builder-spec.json": json.dumps(SPEC).encode()}, modes={"builder-spec.json": "160000"}))
    assert rules_of(doc) == ["unreadable:builder-spec.json"]
    assert run(repo_at(FakeGitHub(permissions=READ_PERMISSIONS), "acme/notes", {}))["signals"][0]["line"] == "No builder-spec.json in the repository at this commit."
