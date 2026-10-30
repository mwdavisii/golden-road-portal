import json

import httpx
import pytest
from portal_fakes import FakeGitHub, transport, writer_config

from portal.github.client import GitHub
from portal.writer.bypass import check_bypass

R = "/repos/acme/scorecard-factory"
D = "/repos/acme/golden-road-deploy"


def as_deploy(doc):
    """A factory ruleset as golden-road-deploy has it: the same shape, requiring deploy-check."""
    return json.loads(json.dumps(doc).replace("factory-check", "deploy-check").replace("scorecard-factory", "golden-road-deploy"))


def recorded(edit=None, deploy_edit=None):
    """Both targets' recorded rulesets. `edit` changes both repos' (the deploy repo's are the factory's, requiring deploy-check);
    `deploy_edit` changes only the deploy repo's."""
    gh = FakeGitHub()
    docs = {name: gh.fixture(name) for name in ("installation", "installation_repositories", "rulesets_list", "ruleset_checks", "ruleset_review")}
    if edit:
        edit(docs)
    deploy = {name: as_deploy(docs[name]) for name in ("rulesets_list", "ruleset_checks", "ruleset_review")}
    if deploy_edit:
        deploy_edit(deploy)
    gh.on("GET", "/app/installations/777", (200, docs["installation"]))
    gh.on("GET", "/installation/repositories", (200, docs["installation_repositories"]))
    for repo, d in ((R, docs), (D, deploy)):
        gh.on("GET", repo + "/rulesets", (200, d["rulesets_list"]))
        gh.on("GET", repo + "/rulesets/101", (200, d["ruleset_checks"]))
        gh.on("GET", repo + "/rulesets/102", (200, d["ruleset_review"]))
    return gh


def run(gh):
    c = GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "token")
    return check_bypass(c, c, 777, writer_config())


def test_the_expected_shape_passes():
    gh = recorded()
    assert run(gh) == []
    assert gh.called("GET", R + "/rulesets")[0]["query"]["includes_parents"] == "true"
    assert gh.called("GET", R + "/rulesets/102")[0]["query"]["includes_parents"] == "true"


@pytest.mark.parametrize("edit, needle", [
    (lambda d: d["ruleset_review"]["bypass_actors"][0].update(bypass_mode="always"), "bypass_mode"),
    (lambda d: d["ruleset_checks"]["bypass_actors"].append({"actor_id": 424242, "actor_type": "Integration", "bypass_mode": "pull_request"}),
     "status checks"),
    (lambda d: d["ruleset_review"]["rules"].append({"type": "non_fast_forward"}), "only a pull_request rule"),
    (lambda d: d["ruleset_review"]["conditions"]["ref_name"].update(include=["~ALL"]), "default branch"),
    (lambda d: d["ruleset_review"].update(target="push"), "default branch"),
    (lambda d: d["ruleset_review"].pop("bypass_actors"), "can't read the bypass actors"),
    (lambda d: d["installation"]["permissions"].update(workflows="write"), "permissions"),
    (lambda d: d["installation"]["permissions"].update(administration="write"), "permissions"),
    (lambda d: d["installation"].update(repository_selection="all"), "selected repositories"),
    (lambda d: d["installation_repositories"]["repositories"].append({"full_name": "acme/hello-app"}), "acme/hello-app"),
    (lambda d: d["ruleset_checks"]["rules"].pop(), "factory-check"),
    (lambda d: d["ruleset_checks"].update(enforcement="evaluate"), "factory-check"),
    (lambda d: d["ruleset_review"]["conditions"]["ref_name"].update(exclude=["~DEFAULT_BRANCH"]), "default branch"),
    (lambda d: d["ruleset_checks"]["rules"][2]["parameters"]["required_status_checks"][0].update(integration_id=1), "factory-check"),
    (lambda d: d["installation_repositories"].update(repositories=[]), "acme/scorecard-factory"),
])
def test_a_bypass_wider_than_the_allowlist_is_a_problem(edit, needle):
    problems = run(recorded(edit))
    assert problems and any(needle in p for p in problems), problems


def test_an_unreadable_ruleset_is_a_problem_not_a_pass():
    gh = recorded()
    gh.routes[("GET", "api.github.com", R + "/rulesets/102")] = [(403, {"message": "Resource not accessible"})]
    assert any("can't read" in p for p in run(gh))


def test_an_unreadable_installation_is_a_problem_not_a_pass():
    gh = recorded()
    gh.routes[("GET", "api.github.com", "/app/installations/777")] = [(401, {"message": "Bad credentials"})]
    problems = run(gh)
    assert problems and all("Bad credentials" not in p for p in problems)


def test_github_text_never_reaches_a_problem():
    def edit(d):
        d["installation_repositories"]["repositories"].append({"full_name": "<script>alert(1)</script>"})
        d["ruleset_review"]["name"] = "<img src=x>"
    problems = run(recorded(edit))
    assert problems and all("<" not in p for p in problems), problems


def test_an_app_that_is_not_the_one_named_is_not_counted_as_the_writer():
    """A bypass for another integration is not this App's; the App's own id is the writer config's."""
    def edit(d):
        d["ruleset_review"]["bypass_actors"][0]["actor_id"] = 1
    problems = run(recorded(edit))
    assert problems == []


@pytest.mark.parametrize("edit, needle", [
    (lambda d: d["ruleset_review"].update(enforcement="disabled", bypass_actors=[{"actor_id": 424242, "actor_type": "Integration", "bypass_mode": "always"}]),
     "bypass_mode"),
    (lambda d: d["ruleset_review"].update(enforcement="evaluate", rules=[{"type": "pull_request"}, {"type": "deletion"}]), "only a pull_request rule"),
    (lambda d: d["ruleset_checks"].update(enforcement="disabled", bypass_actors=[{"actor_id": 424242, "actor_type": "Integration", "bypass_mode": "pull_request"}]),
     "status checks"),
], ids=["disabled-always", "evaluate-wider-rules", "disabled-checks-bypass"])
def test_the_bypass_is_judged_on_every_ruleset_whatever_its_enforcement(edit, needle):
    """A ruleset that is off or only evaluating today can be switched on by an admin tomorrow: its bypass counts now."""
    problems = run(recorded(edit))
    assert any(needle in p for p in problems), problems


@pytest.mark.parametrize("ruleset, value, needle", [
    ("ruleset_review", "always", "current_user_can_bypass"), ("ruleset_review", "exempt", "current_user_can_bypass"),
    ("ruleset_checks", "pull_requests_only", "status checks"), ("ruleset_checks", "always", "status checks"),
    ("ruleset_checks", "exempt", "status checks"),
])
def test_the_ruleset_s_own_word_on_whether_the_app_can_bypass_it_is_believed_when_it_is_there(ruleset, value, needle):
    problems = run(recorded(lambda d: d[ruleset].update(current_user_can_bypass=value)))
    assert any(needle in p for p in problems), problems


@pytest.mark.parametrize("ruleset, value", [("ruleset_review", "pull_requests_only"), ("ruleset_review", "never"), ("ruleset_checks", "never")])
def test_the_expected_current_user_can_bypass_values_pass(ruleset, value):
    assert run(recorded(lambda d: d[ruleset].update(current_user_can_bypass=value))) == []


def test_a_write_installation_with_actions_read_passes_and_with_actions_write_does_not():
    """The writer traces a check run to its workflow run (GET /actions/runs), which needs Actions: read on a private repo."""
    assert run(recorded(lambda d: d["installation"]["permissions"].update(actions="read"))) == []
    problems = run(recorded(lambda d: d["installation"]["permissions"].update(actions="write")))
    assert any("permissions" in p and "actions" in p for p in problems), problems


def test_the_allowed_permissions_cover_every_endpoint_the_writer_calls():
    """A permission the writer needs but the startup check forbids would make it 403 forever or refuse to start."""
    from portal.writer.bypass import ALLOWED_PERMISSIONS
    from portal_fakes import PERMISSION_LEVELS, PERMISSION_RULES
    for method, pattern, permission, level in PERMISSION_RULES:
        assert permission in ALLOWED_PERMISSIONS and PERMISSION_LEVELS[ALLOWED_PERMISSIONS[permission]] >= PERMISSION_LEVELS[level], (method, pattern)
    assert set(ALLOWED_PERMISSIONS) == {permission for _, _, permission, _ in PERMISSION_RULES}, "nothing is allowed that the writer does not use"


def test_the_recorded_github_refuses_a_call_the_installation_has_no_permission_for():
    import httpx as _httpx
    narrow = FakeGitHub(permissions={"contents": "write", "pull_requests": "write", "metadata": "read", "checks": "read", "administration": "read"})
    narrow.on("GET", R + "/actions/runs", (200, {"workflow_runs": []}))
    c = GitHub("https://api.github.com", _httpx.Client(transport=transport(narrow)), lambda: "token")
    from portal.github.client import GitHubError
    with pytest.raises(GitHubError) as err:
        c.call("GET", R + "/actions/runs", params={"check_suite_id": 1})
    assert err.value.status == 403
    assert FakeGitHub().routes == {} and FakeGitHub().permissions["actions"] == "read"


def as_the_app_sees_it(checks_bypass, review_bypass):
    """GitHub hides bypass_actors from a caller that can't edit the ruleset (the write App has administration: read) and gives
    current_user_can_bypass instead: GitHub's own word on whether this caller can bypass the ruleset."""
    def edit(d):
        for name, value in (("ruleset_checks", checks_bypass), ("ruleset_review", review_bypass)):
            d[name].pop("bypass_actors", None)
            if value is None:
                d[name].pop("current_user_can_bypass", None)
            else:
                d[name]["current_user_can_bypass"] = value
    return edit


def test_without_bypass_actors_github_s_word_for_the_app_is_enough():
    assert run(recorded(as_the_app_sees_it("never", "pull_requests_only"))) == []


@pytest.mark.parametrize("checks, review, needle", [
    (None, "pull_requests_only", "can't read the bypass actors"),       # neither the list nor GitHub's word: unchecked
    ("never", None, "can't read the bypass actors"),
    ("pull_requests_only", "pull_requests_only", "bypass status checks"),  # the checks ruleset must be never
    ("always", "pull_requests_only", "bypass status checks"),
    ("never", "always", "current_user_can_bypass"),                     # more than a pull request bypass on the review ruleset
    ("never", "exempt", "current_user_can_bypass"),
])
def test_without_bypass_actors_anything_but_never_and_pull_requests_only_is_a_problem(checks, review, needle):
    problems = run(recorded(as_the_app_sees_it(checks, review)))
    assert any(needle in p for p in problems), problems


def test_a_pull_request_bypass_github_reports_still_needs_the_review_ruleset_s_shape():
    def edit(d):
        as_the_app_sees_it("never", "pull_requests_only")(d)
        d["ruleset_review"]["conditions"]["ref_name"]["include"] = ["~ALL"]
    assert any("only the default branch" in p for p in run(recorded(edit)))



# ---- golden-road-deploy (spec 2026-10-29 GitOps deploy repo, G9): the writer's second target, guarded by deploy-check ----

def test_both_targets_are_checked():
    gh = recorded()
    assert run(gh) == []
    assert gh.called("GET", D + "/rulesets") and gh.called("GET", D + "/rulesets/101")


@pytest.mark.parametrize("deploy_edit, needle", [
    (lambda d: d["ruleset_checks"]["rules"].pop(), "acme/golden-road-deploy has no active default-branch ruleset"),
    (lambda d: d["ruleset_checks"]["rules"][2]["parameters"]["required_status_checks"][0].update(context="factory-check"), "requires deploy-check"),
    (lambda d: d["ruleset_review"]["bypass_actors"][0].update(bypass_mode="always"), "bypass_mode"),
    (lambda d: d["ruleset_checks"]["bypass_actors"].append({"actor_id": 424242, "actor_type": "Integration", "bypass_mode": "pull_request"}),
     "status checks"),
])
def test_the_deploy_repo_must_require_deploy_check_and_bypass_only_its_review_ruleset(deploy_edit, needle):
    problems = run(recorded(deploy_edit=deploy_edit))
    assert any(needle in p for p in problems), problems
    assert all("scorecard-factory" not in p for p in problems), "the factory's rulesets are unchanged"


def test_an_installation_that_does_not_cover_the_deploy_repo_is_a_problem():
    problems = run(recorded(lambda d: d["installation_repositories"].update(repositories=[{"full_name": "acme/scorecard-factory"}])))
    assert problems == ["the write installation does not cover acme/golden-road-deploy"]


def test_a_target_with_no_required_check_named_is_a_problem_not_a_pass():
    c = GitHub("https://api.github.com", httpx.Client(transport=transport(recorded())), lambda: "token")
    problems = check_bypass(c, c, 777, writer_config(deploy_checks=()))
    assert "no required check is named for acme/golden-road-deploy" in problems
