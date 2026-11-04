"""Fakes for the portal's tests: constants, a clock, a recorded GitHub and an OIDC provider behind one httpx transport.
No test reaches the network: an unanswered call raises and fails the test. They live here, not in conftest.py, because this
repo's suite has several conftest.py files and `from conftest import ...` would find the wrong one (decision D2)."""

import base64
import datetime as dt
import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

T0 = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)
ISSUER = "https://sso.example.test/application/o/portal/"
BASE = "https://portal.example.test"
GROUPS = {"anonymous": None, "owner": [], "other": [], "ops": ["golden-road-ops"], "admin": ["golden-road-admins"]}
PRINCIPALS = tuple(GROUPS)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


def transport(*fakes):
    """One MockTransport for several fakes, each answering for its own hosts."""
    def handle(request: httpx.Request) -> httpx.Response:
        for fake in fakes:
            if request.url.host in fake.hosts:
                return fake.handle(request)
        raise AssertionError(f"no fake answers for {request.method} {request.url}")
    return httpx.MockTransport(handle)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_rsa_key():
    """A fresh key per test run: no private key is ever committed (scanners would flag it)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key) -> bytes:
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


class FakeIdP:
    """An OIDC provider: discovery, JWKS, and a token endpoint that checks the client's credentials, the code and PKCE."""

    def __init__(self, issuer, client_id="portal-client", client_secret="oidc-secret-for-tests"):
        self.issuer, self.client_id, self.client_secret = issuer, client_id, client_secret
        self.host = urlsplit(issuer).hostname
        self.hosts = {self.host}
        self.base = urlsplit(issuer).path.rstrip("/")
        self.key, self.kid = new_rsa_key(), "test-key-1"
        self.codes, self.override, self.token_factory = {}, {}, None

    def authorize(self, url, *, sub, groups, name="Pat Example", email="pat@example.test", email_verified=True):
        """What the browser does at the authorization endpoint: returns the code the provider redirects back with."""
        q = parse_qs(urlsplit(url).query)
        assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"] and q["client_id"] == [self.client_id]
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": q["code_challenge"][0], "redirect_uri": q["redirect_uri"][0], "nonce": q["nonce"][0],
                            "claims": {"sub": sub, "groups": groups, "name": name, "email": email,
                                       **({} if email_verified is None else {"email_verified": email_verified})}}
        return code

    def id_token(self, claims, *, alg="RS256", key=None, kid=None):
        now = int(time.time())
        body = {"iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300, **claims}
        if alg == "none":  # PyJWT may refuse to encode it, and the point is that the portal refuses to read it
            header = b64url(json.dumps({"alg": "none", "kid": kid or self.kid}).encode())
            return header + "." + b64url(json.dumps(body).encode()) + "."
        signing = key if alg.startswith("HS") else pem(key or self.key)
        return jwt.encode(body, signing, algorithm=alg, headers={"kid": kid or self.kid})

    def handle(self, request):
        path = request.url.path
        # any issuer path answers with this provider's own document, as a provider behind a rename or a wrong URL would
        if request.method == "GET" and path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": self.issuer, "authorization_endpoint": f"https://{self.host}/application/o/authorize/",
                                             "token_endpoint": f"https://{self.host}/application/o/token/", "jwks_uri": f"https://{self.host}{self.base}/jwks/"})
        if request.method == "GET" and path == self.base + "/jwks/":
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]})
        if request.method == "POST" and path == "/application/o/token/":
            expected = "Basic " + base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            entry = self.codes.pop(form.get("code", ""), None)
            if entry is None or form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != entry["redirect_uri"] \
                    or b64url(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != entry["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {**entry["claims"], "nonce": entry["nonce"], **self.override}
            token = self.token_factory(claims) if self.token_factory else self.id_token(claims)
            return httpx.Response(200, json={"access_token": "unused", "token_type": "Bearer", "id_token": token})
        raise AssertionError(f"FakeIdP has no answer for {request.method} {request.url}")


FIXTURES = Path(__file__).resolve().parent / "fixtures"


# What an installation token needs for each endpoint the writer uses (GitHub's "permissions required for fine-grained tokens"): a call
# the installation's repository permissions don't cover gets 403, as GitHub answers, so a missing permission fails a test.
PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}
REPO_PATH = r"/repos/[^/]+/[^/]+"
PERMISSION_RULES = (
    ("GET", rf"{REPO_PATH}", "metadata", "read"),
    ("GET", rf"{REPO_PATH}/(contents/.*|git/ref/.*|commits/[0-9a-f]{{40}})", "contents", "read"),
    ("GET", rf"{REPO_PATH}/git/blobs/[0-9a-f]{{40}}", "contents", "read"),
    ("GET", rf"{REPO_PATH}/compare/.+", "contents", "read"),
    ("GET", rf"{REPO_PATH}/git/trees/[0-9a-f]{{40}}", "contents", "read"),
    ("PUT", rf"{REPO_PATH}/contents/.*", "contents", "write"),
    ("DELETE", rf"{REPO_PATH}/contents/.*", "contents", "write"),  # a decommission's deletions, a resume's lifecycle.yaml (spec 2026-11-02)
    ("PATCH", rf"{REPO_PATH}", "administration", "write"),  # the archive: the lifecycle App's only call, never the write App's (L9)
    ("POST", rf"{REPO_PATH}/git/refs", "contents", "write"),
    ("GET", rf"{REPO_PATH}/(pulls|pulls/\d+|pulls/\d+/files)", "pull_requests", "read"),
    ("POST", rf"{REPO_PATH}/pulls", "pull_requests", "write"),
    ("PATCH", rf"{REPO_PATH}/pulls/\d+", "pull_requests", "write"),  # a failed request's PR closed, an adopted PR's body updated
    ("POST", rf"{REPO_PATH}/issues/\d+/comments", "pull_requests", "write"),  # on a pull request, pull_requests write is enough
    ("PUT", rf"{REPO_PATH}/pulls/\d+/merge", "contents", "write"),
    ("GET", rf"{REPO_PATH}/commits/[0-9a-f]{{40}}/check-runs", "checks", "read"),
    ("GET", rf"{REPO_PATH}/actions/runs", "actions", "read"),
    ("GET", rf"{REPO_PATH}/actions/workflows/[^/]+/runs", "actions", "read"),
    ("GET", rf"{REPO_PATH}/actions/runs/\d+/artifacts", "actions", "read"),
    ("GET", rf"{REPO_PATH}/actions/runs/\d+", "actions", "read"),
    ("GET", rf"{REPO_PATH}/actions/artifacts/\d+/zip", "actions", "read"),
    ("GET", rf"{REPO_PATH}/rulesets(/\d+)?", "administration", "read"),
)
LIFECYCLE_APP_ONLY = (("PATCH", rf"{REPO_PATH}"),)  # the rules the write App must never meet: only the lifecycle App archives (L9)


class FakeGitHub:
    """A recorded GitHub. Answers are queued per (METHOD, host, path): (status, body), (status, body, headers) or a
    callable(request). Recorded responses live in tests/fixtures/github/, trimmed to the fields the portal reads. The last
    answer repeats. Every call is logged, and a call with no answer fails the test."""

    def __init__(self, api_host="api.github.com", web_host="github.com", permissions=None):
        self.api_host, self.web_host = api_host, web_host
        self.hosts = {api_host, web_host}
        self.routes, self.calls, self.fallbacks = {}, [], []
        # the write installation's permissions (the recorded installation's, unless a test narrows them)
        self.permissions = dict(self.fixture("installation")["permissions"] if permissions is None else permissions)

    def permitted(self, request) -> bool:
        if request.url.host != self.api_host:
            return True
        for method, pattern, permission, level in PERMISSION_RULES:
            if request.method == method and re.fullmatch(pattern, request.url.path):
                return PERMISSION_LEVELS.get(self.permissions.get(permission), 0) >= PERMISSION_LEVELS[level]
        return True

    def on(self, method, path, *answers, host=None):
        self.routes.setdefault((method, host or self.api_host, path), []).extend(answers)
        return self

    def on_any(self, method, pattern, answer, host=None):
        """One answer for every path that matches `pattern` (a regex) and has no answer of its own: a 404 for files a test doesn't name."""
        self.fallbacks.append((method, host or self.api_host, re.compile(pattern), answer))
        return self

    @staticmethod
    def fixture(name):
        return json.loads((FIXTURES / "github" / f"{name}.json").read_text())

    def handle(self, request):
        key = (request.method, request.url.host, request.url.path)
        self.calls.append({"method": request.method, "host": request.url.host, "path": request.url.path,
                           "query": dict(request.url.params), "body": request.content, "headers": dict(request.headers)})
        if not self.permitted(request):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        answers = self.routes.get(key)
        if not answers:
            found = next((a for m, h, rx, a in self.fallbacks if (m, h) == key[:2] and rx.fullmatch(key[2])), None)
            if found is None:
                raise AssertionError(f"FakeGitHub has no answer for {request.method} {request.url}")
            answers = [found]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer):
            return answer(request)
        status, body, *rest = answer
        return httpx.Response(status, json=body, headers=rest[0] if rest else None)

    def called(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


def approved_request(store, people, now, repo="acme/notes", claim=True):
    """A registration by owner, approved by admin with the record the web would build, claimed by the writer (state writing), or
    left approved with claim=False."""
    from portal.registration import intake_record
    # the portal fixture already seeds a pending registration of acme/notes by owner: use it rather than open a second one
    req = next((r for r in store.requests_by(people["owner"].id) if r.repo == repo and r.state == "pending"), None) or store.create_registration(
        person_id=people["owner"].id, now=now, draft={"schema": "registration-draft/v1", "repo": repo,
                                                      "purpose": "Stores short team notes; no email, no files.",
                                                      "data_classification": "public"})
    evidence = store.evidence_for(req.id, req.submission) or give_evidence(store, req, now=now)
    final, errors = intake_record(req, approver_id=people["admin"].id, tier="moderate", classification="public", review_by="2027-10-23",
                                  justification="Purpose and classification checked.", now=now, evidence=evidence)
    assert not errors
    store.decide(request_id=req.id, actor_id=people["admin"].id, actor_groups=["golden-road-admins"], approve=True,
                 note=final["approval_justification"], final=final, now=now)
    return store.claim_approved(now=now) if claim else store.get_request(req.id)


def mock_cloudflare(email="owner@example.test", doc=None):
    """The mock Cloudflare (mock_cloudflare.json, or `doc`) with the policy's mwdavisii@example.com given to `email`: the portal
    fixture's owner signs in as owner@example.test, as the secrets tests swap the Secret Server login."""
    from portal.cloudflare_mock import MOCK, parse
    doc = json.loads(MOCK.read_text(encoding="utf-8")) if doc is None else doc
    for user in doc["users"]:
        if user["email"] == "mwdavisii@example.com":
            user["email"] = email
    return parse(doc)


def mock_servicenow(org="acme", ci="notes", doc=None):
    """The mock ServiceNow (mock_servicenow.json, or `doc`) for `org`, with hello-app's tasks naming `ci` instead (the portal fixture's app
    is acme/notes), as mock_cloudflare swaps the policy's email."""
    from portal.servicenow_mock import MOCK, parse
    doc = json.loads(MOCK.read_text(encoding="utf-8")) if doc is None else doc
    for task in doc["tasks"]:
        if task["ci"] == "hello-app":
            task["ci"] = ci
    return parse(doc, org=org)


def binding_request(store, people, now, repo="acme/notes", databricks=True, current=None, environment="test", identity=True,
                    secrets=(), mcp=(), skills=()):
    """A binding by owner (an admin of acme/notes as seeded) for one environment: sp-notes unless identity is False, wh-notes unless
    databricks (or identity) is False, each of `secrets` ((secret id, field slug, env) picked by the mock user mwdavisii in the
    mock Secret Server), each of `mcp` (a server id on the mock Cloudflare's gateway, picked by owner@example.test) and each of
    `skills` ((name, commit): picked from the mock catalog at its main, then pinned at `commit`), built the way the app page builds
    it, then claimed by the writer (state writing)."""
    from portal.binding_record import next_binding
    from portal.ids import new_id
    rid = new_id("r")
    doc = current
    common = dict(repo=repo, org="acme", directory=fake_directory(), person_id=people["owner"].id, request_id=rid, now=now,
                  environment=environment)
    if secrets:
        from portal.secretserver_mock import load_mock
        server = load_mock()
        for secret_id, slug, env in secrets:
            doc = next_binding(current=doc, **common, add_secret={"secret_id": secret_id, "field_slug": slug, "env": env},
                               secret_server=server, secret_user="mwdavisii")
    if mcp:
        cloudflare = mock_cloudflare()
        for server_id in mcp:
            doc = next_binding(current=doc, **common, add_mcp=server_id, cloudflare=cloudflare, mcp_gateway=cloudflare.gateways[environment],
                               person_email=people["owner"].email)
    if skills:
        from portal.skills_catalog import load_mock as load_catalog
        catalog = load_catalog()
        for name, commit in skills:
            doc = next_binding(current=doc, **common, add_skill=name, skills_catalog=catalog)
            doc["skills"] = [{**s, "commit": commit} if s["name"] == name else s for s in doc["skills"]]
    if identity:
        doc = next_binding(current=doc, **common, principal_id="sp-notes")
    if identity and databricks:
        doc = next_binding(current=doc, repo=repo, org="acme", directory=fake_directory(), person_id=people["owner"].id, request_id=rid,
                           now=now, environment=environment, warehouse_id="wh-notes")
    store.create_binding(request_id=rid, person_id=people["owner"].id, final=doc, now=now, current=current)
    claimed = store.claim_approved(now=now)
    assert claimed.id == rid
    return claimed


def writer_config(**changes):
    from portal.writer.config import WriterConfig
    base = dict(org="acme", role_groups={"admin": frozenset({"golden-road-admins"})}, app_id=424242, write_login="golden-road-portal-writer[bot]")
    return WriterConfig(**{**base, **changes})


def make_settings(**changes):
    from portal.settings import Settings
    base = dict(base_url=BASE, org="acme", github_host="github.com", oidc_issuer=ISSUER, oidc_client_id="portal-client",
                oidc_client_secret="oidc-secret-for-tests",
                role_groups={"admin": frozenset({"golden-road-admins"}), "ops": frozenset({"golden-road-ops"})},
                github_oauth_client_id="Iv1.testclient", github_oauth_client_secret="gh-secret-for-tests", database_url="postgresql://unused")
    return Settings(**{**base, **changes})


# A small directory for the tests (mock-directory/v1): sp-notes is tagged for acme/notes and granted wh-notes; sp-other and
# wh-other belong to acme/other.
FAKE_DIRECTORY = {
    "schema": "mock-directory/v1",
    "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
    "principals": [
        {"id": "sp-notes", "display_name": "sp-notes", "client_id": "11111111-1111-4111-8111-111111111111", "tagged_repo_name": "notes"},
        {"id": "sp-other", "display_name": "sp-other", "client_id": "33333333-3333-4333-8333-333333333333", "tagged_repo_name": "other"},
    ],
    "databricks": [
        {"id": "wh-notes", "display_name": "Notes", "host": "adb-1111111111111111.1.azuredatabricks.net",
         "http_path": "/sql/1.0/warehouses/0000notes0000", "catalog": "notes", "schema": "public", "granted_to": ["sp-notes"]},
        {"id": "wh-other", "display_name": "Other", "host": "adb-2222222222222222.2.azuredatabricks.net",
         "http_path": "/sql/1.0/warehouses/0000other0000", "granted_to": ["sp-other"]},
    ],
}


def fake_directory():
    from portal.directory import parse
    return parse(FAKE_DIRECTORY)


def seed(store, people, now):
    """What every portal test starts from: owner linked to GitHub login owner-gh (id 1001), an admin of acme/notes, and a
    pending registration of acme/notes by owner."""
    from portal.store.base import RepoAdmin
    link = store.add_link(person_id=people["owner"].id, github_login="owner-gh", github_user_id=1001, source="admin",
                          actor_id=people["admin"].id, reason="seeded for the tests", now=now)
    store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, now), RepoAdmin("acme/tools", "owner-gh", 1001, now)], observed_at=now)
    request = store.create_registration(person_id=people["owner"].id, now=now, draft={
        "schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes; no email, no files.",
        "data_classification": "public"})
    give_evidence(store, request, now=now)  # the platform's evidence at the floor low/public, so an admin can approve
    return {"link": link.id, "request": request.id, "owner": people["owner"].id}


# The factory's config/registration.yaml as a dict (registration-rules/v1): what the floor tests and the collector tests use.
RULES = {"schema": "registration-rules/v1", "no_spec": {"tier": "high", "data_classification": "confidential"},
         "classification_tier": {"public": "low", "confidential": "high", "protected": "high"},
         "raise_to": {"write_roles": "moderate", "secret_env": "moderate", "model_gateway_egress": "moderate",
                      "other_egress": "moderate", "many_dependencies": "moderate"},
         "many_dependencies_over": 50}


def intake_trees(gh, repo, commit, records, *, root=(), owner_entries=None, truncated=False):
    """The Git Trees API at `commit`, from the root tree down to each intake/<owner>/<name>.yaml in `records`: the root (with `root`'s
    entries), intake/ and each owner's tree. Each route answers this, replacing any earlier answer. owner_entries adds entries to every
    owner tree (a big directory); truncated marks the owner trees truncated, as GitHub does past its limit."""
    owners = sorted({p.split("/")[1] for p in records})
    intake_sha = git_sha(f"tree {repo} {commit} intake".encode())
    entries = list(root) + ([{"path": "intake", "mode": "040000", "type": "tree", "sha": intake_sha}] if owners else [])
    gh.routes[("GET", gh.api_host, f"/repos/{repo}/git/trees/{commit}")] = [(200, {"sha": commit, "truncated": False, "tree": entries})]
    if not owners:
        return gh
    sha_of = {o: git_sha(f"tree {repo} {commit} intake/{o}".encode()) for o in owners}
    gh.routes[("GET", gh.api_host, f"/repos/{repo}/git/trees/{intake_sha}")] = [(200, {"sha": intake_sha, "truncated": False, "tree": [
        {"path": o, "mode": "040000", "type": "tree", "sha": sha_of[o]} for o in owners]})]
    for o in owners:
        names = [{"path": p.rsplit("/", 1)[1], "mode": "100644", "type": "blob", "sha": "b" * 40} for p in records if p.split("/")[1] == o]
        gh.routes[("GET", gh.api_host, f"/repos/{repo}/git/trees/{sha_of[o]}")] = [
            (200, {"sha": sha_of[o], "truncated": truncated, "tree": list(owner_entries or []) + names})]
    return gh


def git_sha(raw: bytes) -> str:
    """The blob SHA git (and the contents API's "sha") gives these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()


# What the read installation is granted (the collector's App): Metadata and Contents, read only. A call needing more gets 403.
READ_PERMISSIONS = {"metadata": "read", "contents": "read"}
READ_WITH_ACTIONS = {**READ_PERMISSIONS, "actions": "read"}  # the read App from 0.5.1: it lists scorecard runs and downloads their artifacts
KNOWN_FILES = ("builder-spec.json", ".builder.lock", "requirements.txt", "package-lock.json", "config/registration.yaml")


def repo_at(gh, repo, files, *, commit="a" * 40, branch="main", modes=None):
    """Register a repo's default branch, its head at `commit` and its files on `gh`. A file is bytes (a 200 with base64 content), an int
    (that status) or a dict (used as the body). Known paths that `files` leaves out answer 404."""
    gh.on("GET", f"/repos/{repo}", (200, {"default_branch": branch}))
    gh.on("GET", f"/repos/{repo}/git/ref/heads/{branch}", (200, {"object": {"sha": commit}}))
    # the commit's root tree: each root file is a regular file unless `modes` says otherwise (120000 symlink, 160000 submodule)
    root = [{"path": p, "mode": (modes or {}).get(p, "100644"), "type": "blob", "sha": "b" * 40} for p in sorted(files) if "/" not in p]
    intake_trees(gh, repo, commit, [p for p in files if p.startswith("intake/")], root=root)  # the root tree, down to each named record
    gh.on_any("GET", rf"/repos/{re.escape(repo)}/contents/intake/.+", (404, {"message": "Not Found"}))  # no intake record unless named
    for path in sorted(set(KNOWN_FILES) | set(files)):
        entry = files.get(path)
        if entry is None:
            answer = (404, {"message": "Not Found"})
        elif isinstance(entry, bytes):
            answer = (200, {"type": "file", "encoding": "base64", "size": len(entry), "content": base64.b64encode(entry).decode(),
                            "sha": git_sha(entry)})
        elif isinstance(entry, int):
            answer = (entry, {"message": "status"})
        else:
            answer = (200, entry)
        gh.on("GET", f"/repos/{repo}/contents/{path}", answer)
    return gh


def evidence_doc(repo, commit="e" * 40, **changes):
    """A valid registration-evidence/v1 for `repo` with a spec, no signals and nothing noteworthy."""
    doc = {"schema": "registration-evidence/v1", "repo": repo, "commit": commit, "spec_present": True, "data_classification": "public",
           "app_type": "python", "resources_tier": "default", "egress": [], "secret_env": [], "write_roles": [], "write_routes": 0,
           "builder_version": "0.3.10", "dependencies": {"count": 3, "lockfile_present": True, "lockfile": "requirements.txt"}, "signals": []}
    return {**doc, **changes}


def give_evidence(store, request, *, floor_tier="low", floor_classification="public", commit="e" * 40, now=T0, record_sha=None, record=None,
                  **changes):
    """Record the collector's evidence for `request`'s current submission, as the collector would (with the record a change replaces)."""
    return store.add_evidence(request_id=request.id, submission=request.submission, commit=commit,
                              evidence=evidence_doc(request.repo, commit, **changes), floor_tier=floor_tier,
                              floor_classification=floor_classification, rules_commit="f" * 40, now=now, record_sha=record_sha, record=record)


def record_view(owner, **changes):
    """The collector's view of a portal-written intake/v2 record of acme/tools (collectors.intake.parse_record's shape)."""
    return {"schema": "intake/v2", "by_portal": True, "tier": "moderate", "data_classification": "public",
            "purpose": "Builds internal tools; no PHI, no email.", "owner": owner, "review_by": "2026-11-15", **changes}


def approved_change(store, people, now, *, record, record_sha="c" * 40, repo="acme/tools", tier="moderate", classification="public",
                    purpose="Builds internal tools; no PHI, no email.", review_by="2027-10-23", requester="owner", claim=True):
    """A change of `repo`'s registration (its evidence names `record`), approved by admin and claimed by the writer (state writing)."""
    from portal.registration import intake_record
    req = store.create_registration(person_id=people[requester].id, now=now, draft={
        "schema": "registration-draft/v1", "repo": repo, "purpose": purpose, "data_classification": classification})
    evidence = give_evidence(store, req, now=now, record_sha=record_sha, record=record)
    final, errors = intake_record(req, approver_id=people["admin"].id, tier=tier, classification=classification, review_by=review_by,
                                  justification="Change checked against the current record.", now=now, evidence=evidence)
    assert not errors, errors
    store.decide(request_id=req.id, actor_id=people["admin"].id, actor_groups=["golden-road-admins"], approve=True,
                 note=final["approval_justification"], final=final, now=now)
    return store.claim_approved(now=now) if claim else store.get_request(req.id)


def factory_with_records(gh, records, *, commit="f" * 40, modes=None, org="acme"):
    """The factory's default branch at `commit` holding intake records ({"acme/notes": bytes}): its root tree, the intake tree, each owner's
    tree and each blob, as the git data API answers them. `modes` gives a record another mode (120000 is a symlink)."""
    repo = f"{org}/scorecard-factory"
    gh.on("GET", f"/repos/{repo}", (200, {"default_branch": "main"}))
    gh.on("GET", f"/repos/{repo}/git/ref/heads/main", (200, {"object": {"sha": commit}}))
    owners = sorted({name.split("/")[0] for name in records})
    tree_sha = {o: git_sha(f"tree {o}".encode()) for o in owners}
    gh.on("GET", f"/repos/{repo}/git/trees/{commit}", (200, {"sha": commit, "truncated": False, "tree": [
        {"path": "config", "mode": "040000", "type": "tree", "sha": "0" * 40},
        {"path": "intake", "mode": "040000", "type": "tree", "sha": "1" * 40}]}))
    gh.on("GET", f"/repos/{repo}/git/trees/{'1' * 40}", (200, {"sha": "1" * 40, "truncated": False, "tree": [
        {"path": o, "mode": "040000", "type": "tree", "sha": tree_sha[o]} for o in owners]}))
    for o in owners:
        entries = []
        for name, raw in sorted(records.items()):
            if name.split("/")[0] != o:
                continue
            sha = git_sha(raw)
            entries.append({"path": name.split("/")[1] + ".yaml", "mode": (modes or {}).get(name, "100644"), "type": "blob", "sha": sha})
            gh.on("GET", f"/repos/{repo}/git/blobs/{sha}", (200, {"sha": sha, "encoding": "base64", "size": len(raw),
                                                                  "content": base64.b64encode(raw).decode()}))
        gh.on("GET", f"/repos/{repo}/git/trees/{tree_sha[o]}", (200, {"sha": tree_sha[o], "truncated": False, "tree": entries}))
    return gh


class FakeStorage:
    """Actions' artifact storage, behind GitHub's redirect: answers each stored blob by its path and logs every request with its headers."""

    def __init__(self, host="results.storage.example.test"):
        self.host, self.hosts, self.blobs, self.calls = host, {host}, {}, []

    def put(self, name, data, status=200, headers=None) -> str:
        self.blobs["/" + name] = (status, data, headers or {})
        return f"https://{self.host}/{name}?sig=secret-sas-token"

    def handle(self, request):
        self.calls.append({"path": request.url.path, "headers": dict(request.headers)})
        status, data, headers = self.blobs[request.url.path]
        return httpx.Response(status, stream=httpx.ByteStream(data), headers=headers)  # streamed, as storage answers (iter_raw reads it)


def zipped(entries) -> bytes:
    """A zip held in memory: {name: bytes}."""
    import io
    import zipfile
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return out.getvalue()


# ---- lifecycle (spec 2026-11-02): a stateful GitHub, the deploy repo and factory it starts from, a lifecycle request ----

class RepoWorld:
    """A small stateful GitHub for the lifecycle flows (spec 2026-11-02 Testing): repos on main, branches, and commits that hold whole
    trees ({path: bytes}). It answers the calls the writer makes: the repo, refs, the contents API (GET; PUT and DELETE, one commit
    each), commits with their files, the Git Trees API, pull requests (list, create, read, files, merge, close, comment), check runs and
    the workflow run behind each (every check passes unless `checks` says otherwise), and for the lifecycle App: a repo's installation,
    its token and the archive. A person merges with merge(), pushes with push(), moves main with advance_main(). An unknown call fails.

    Its answers have the shapes writer.run's checks read: a check run names its suite, head_sha and the one PR (_run_is_ours), the
    suite's one workflow run is a pull_request run for the PR's branch and tip from the repo itself, and a commit names its one parent
    and its files (_chain)."""

    def __init__(self, api_host="api.github.com"):
        self.api_host, self.hosts = api_host, {api_host}
        self.repos, self.commits, self.pulls, self.trees = {}, {}, {}, {}
        self.checks = {}  # (repo, check name) -> "success", "failure" or None (still running)
        self.installed = {}  # repo -> the lifecycle App's installation id there
        self.lifecycle_app_id = 424243  # the App whose JWT the installation and token calls need (lifecycle_app() signs as it)
        self.archived = set()
        self.lifecycle_token = "ghs_" + "L" * 36
        self.token_requests = []
        self.token_permissions = None  # what GitHub grants the token (None: what was asked)
        self.revoked = set()  # tokens revoked with DELETE /installation/token
        self.calls = []
        self.comments = []  # (repo, number, body)
        self.writer_login = "golden-road-portal-writer[bot]"  # who opens a PR through the API (the write App's bot)
        self._n = 0

    # ---- set up and act as people ----

    def add_repo(self, repo, files=None):
        self.repos[repo] = {"main": self._new_commit(dict(files or {}), None, [])}
        return self.repos[repo]["main"]

    def head(self, repo, branch="main"):
        return self.repos[repo][branch]

    def files(self, repo, ref="main"):
        return dict(self.commits[self.repos[repo].get(ref, ref)]["tree"])

    def push(self, repo, branch, path, content):
        tree = self.files(repo, branch)
        status = "modified" if path in tree else "added"
        tree[path] = content
        self.repos[repo][branch] = self._new_commit(tree, self.repos[repo][branch], [{"filename": path, "status": status}])

    def advance_main(self, repo, path, content):
        self.push(repo, "main", path, content)

    def merge(self, repo, number):
        """A person merges the PR (squash), as the review rule lets a code owner. The strict ruleset holds a person too: the PR must be
        open and up to date with main (its base the current main), or GitHub refuses the merge."""
        pull = self.pulls[(repo, number)]
        if pull["state"] != "open" or pull["base_sha"] != self.repos[repo]["main"]:
            raise AssertionError(f"GitHub refuses to merge #{number}: {'not open' if pull['state'] != 'open' else 'not up to date with main'}")
        tree = self.files(repo, pull["head"]["ref"])
        sha = self._new_commit(tree, self.repos[repo]["main"], self._diff(repo, pull))
        self.repos[repo]["main"] = sha
        pull.update(state="closed", merged=True, merge_commit_sha=sha, merged_at="2026-11-02T23:00:00Z", last_tip=self.repos[repo][pull["head"]["ref"]])
        return sha

    def pull(self, repo, number):
        return self.pulls[(repo, number)]

    def branches(self, repo):
        return {name for name in self.repos[repo] if name != "main"}

    # ---- the answers ----

    def _new_commit(self, tree, parent, changed):
        self._n += 1
        sha = hashlib.sha1(f"commit {self._n}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parent": parent, "files": changed}
        return sha

    def _ref(self, repo, ref):
        return self.repos[repo].get(ref, ref if ref in self.commits else None)

    def _diff(self, repo, pull):
        base, head = self.files(repo, pull["base_sha"]), self.files(repo, pull["head"]["ref"])
        out = [{"filename": p, "status": "added" if p not in base else "modified"} for p in sorted(head) if base.get(p) != head[p]]
        return out + [{"filename": p, "status": "removed"} for p in sorted(set(base) - set(head))]

    def _tree_sha(self, commit, prefix):
        sha = hashlib.sha1(f"tree {commit} {prefix}".encode()).hexdigest()
        self.trees[sha] = (commit, prefix)
        return sha

    def _tree(self, sha):
        commit, prefix = (sha, "") if sha in self.commits else self.trees[sha]
        entries, seen = [], set()
        for path, content in sorted(self.commits[commit]["tree"].items()):
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1:] if prefix else path
            head, _, tail = rest.partition("/")
            if tail and head not in seen:
                seen.add(head)
                entries.append({"path": head, "mode": "040000", "type": "tree", "sha": self._tree_sha(commit, f"{prefix}/{head}".lstrip("/"))})
            elif not tail:
                entries.append({"path": head, "mode": "100644", "type": "blob", "sha": git_sha(content)})
        return {"sha": sha, "truncated": False, "tree": entries}

    def _as_lifecycle_app(self, request) -> bool:
        """The call carries an RS256 App JWT issued by the lifecycle App (its signature is the real GitHub's to check)."""
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return jwt.get_unverified_header(token).get("alg") == "RS256" and claims.get("iss") == str(self.lifecycle_app_id)
        except (jwt.PyJWTError, IndexError, ValueError):
            return False

    def _pr_view(self, repo, pull):
        # GitHub keeps a merged PR's head, and a PR's last head once its branch is deleted
        tip = pull["last_tip"] if pull.get("merged") else self.repos[repo].get(pull["head"]["ref"], pull.get("last_tip"))
        return {**{k: v for k, v in pull.items() if k not in ("base_sha", "last_tip")}, "head": {**pull["head"], "sha": tip, "repo": {"full_name": repo}},
                "base": {"ref": "main", "sha": pull["base_sha"]},  # the base at the PR's last sync: the writer never updates it
                "mergeable_state": "behind" if pull["base_sha"] != self.repos[repo]["main"] else "clean"}

    def handle(self, request):
        method, path, params = request.method, request.url.path, dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append({"method": method, "path": path, "query": params, "body": body, "auth": request.headers.get("authorization", "")})
        as_app = path.startswith("/app/") or path.endswith("/installation")
        if as_app and not self._as_lifecycle_app(request):
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        if method == "POST" and (m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)):
            self.token_requests.append((int(m.group(1)), body))
            self.revoked.discard(self.lifecycle_token)
            return httpx.Response(201, json={"token": self.lifecycle_token, "expires_at": "2026-11-03T00:00:00Z",
                                             "permissions": self.token_permissions or (body or {}).get("permissions", {}),
                                             "repository_selection": "selected",
                                             "repositories": [{"name": n} for n in (body or {}).get("repositories", [])]})
        if request.headers.get("authorization") in {f"Bearer {t}" for t in self.revoked}:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if method == "DELETE" and path == "/installation/token":
            if request.headers.get("authorization") != f"Bearer {self.lifecycle_token}":
                return httpx.Response(401, json={"message": "Bad credentials"})
            self.revoked.add(self.lifecycle_token)
            return httpx.Response(204)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if m is None or m.group(1) not in self.repos and not path.endswith("/installation"):
            return httpx.Response(404, json={"message": "Not Found"})
        repo, rest = m.group(1), m.group(2) or ""
        if rest == "/installation":
            found = self.installed.get(repo)
            return httpx.Response(200, json={"id": found}) if found else httpx.Response(404, json={"message": "Not Found"})
        if rest == "" and method == "GET":
            return httpx.Response(200, json={"full_name": repo, "default_branch": "main", "archived": repo in self.archived})
        if rest == "" and method == "PATCH":
            if request.headers.get("authorization") != f"Bearer {self.lifecycle_token}" or repo not in self.installed:
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
            if body != {"archived": True}:
                return httpx.Response(422, json={"message": "the fake archives only"})
            self.archived.add(repo)
            return httpx.Response(200, json={"full_name": repo, "archived": True})
        if method == "GET" and (b := re.fullmatch(r"/git/ref/heads/(.+)", rest)):
            sha = self.repos[repo].get(b.group(1))
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404, json={"message": "Not Found"})
        if method == "DELETE" and (b := re.fullmatch(r"/git/refs/heads/(.+)", rest)):
            if b.group(1) == "main" or b.group(1) not in self.repos[repo]:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            for (r, _), pull in self.pulls.items():
                if r == repo and pull["head"]["ref"] == b.group(1) and not pull.get("merged"):
                    pull["last_tip"] = self.repos[repo][b.group(1)]
            del self.repos[repo][b.group(1)]
            return httpx.Response(204)
        if method == "POST" and rest == "/git/refs":
            name = body["ref"].removeprefix("refs/heads/")
            if name in self.repos[repo]:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.repos[repo][name] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if method == "GET" and (t := re.fullmatch(r"/git/trees/([0-9a-f]{40})", rest)):
            return httpx.Response(200, json=self._tree(t.group(1)))
        if method == "GET" and (c := re.fullmatch(r"/commits/([0-9a-f]{40})", rest)):
            commit = self.commits[c.group(1)]
            return httpx.Response(200, json={"sha": c.group(1), "parents": [{"sha": commit["parent"]}] if commit["parent"] else [],
                                             "files": commit["files"]})
        if (f := re.fullmatch(r"/contents/(.+)", rest)):
            file_path = f.group(1)
            if method == "GET":
                ref = self._ref(repo, params.get("ref", "main"))
                tree = self.files(repo, ref) if ref is not None else {}
                if file_path not in tree:
                    return httpx.Response(404, json={"message": "Not Found"})
                raw = tree[file_path]
                return httpx.Response(200, json={"type": "file", "encoding": "base64", "sha": git_sha(raw), "content": base64.b64encode(raw).decode()})
            branch = body["branch"]
            tree = self.files(repo, branch)
            if (body.get("sha") is not None) != (file_path in tree) or (file_path in tree and git_sha(tree[file_path]) != body.get("sha")):
                return httpx.Response(409 if file_path in tree else 422, json={"message": "sha does not match"})
            if method == "DELETE":
                del tree[file_path]
                status = "removed"
            else:
                status = "modified" if file_path in tree else "added"
                tree[file_path] = base64.b64decode(body["content"])
            sha = self._new_commit(tree, self.repos[repo][branch], [{"filename": file_path, "status": status}])
            self.repos[repo][branch] = sha
            return httpx.Response(200 if status != "added" else 201,
                                  json={"commit": {"sha": sha}, "content": None if method == "DELETE" else {"sha": git_sha(tree[file_path])}})
        if rest == "/pulls" and method == "GET":
            head = params.get("head", "").split(":", 1)[-1]
            # GitHub's list answers the pull-request-simple shape: merged_at and merge_commit_sha, but no "merged" (only GET /pulls/{n} has it)
            return httpx.Response(200, json=[{k: v for k, v in self._pr_view(repo, p).items() if k != "merged"}
                                             for (r, _), p in sorted(self.pulls.items()) if r == repo and p["head"]["ref"] == head])
        if rest == "/pulls" and method == "POST":
            number = 1 + max([n for (r, n) in self.pulls if r == repo], default=0)
            self.pulls[(repo, number)] = {"number": number, "html_url": f"https://github.com/{repo}/pull/{number}", "state": "open", "merged": False,
                                          "merged_at": None, "merge_commit_sha": None,
                                          "title": body["title"], "body": body["body"], "head": {"ref": body["head"]}, "base_sha": self.repos[repo]["main"],
                                          "user": {"login": self.writer_login, "type": "Bot"}}
            return httpx.Response(201, json=self._pr_view(repo, self.pulls[(repo, number)]))
        if (p := re.fullmatch(r"/pulls/(\d+)(/files|/merge)?", rest)):
            pull = self.pulls[(repo, int(p.group(1)))]
            if p.group(2) == "/files":
                return httpx.Response(200, json=self._diff(repo, pull))
            if p.group(2) == "/merge":
                if pull["state"] != "open" or pull["base_sha"] != self.repos[repo]["main"]:
                    return httpx.Response(405, json={"message": "Pull Request is not mergeable"})
                if body.get("sha") != self.repos[repo][pull["head"]["ref"]]:
                    return httpx.Response(409, json={"message": "Head branch was modified"})
                return httpx.Response(200, json={"sha": self.merge(repo, pull["number"]), "merged": True})
            if method == "PATCH":
                if pull["merged"] and "state" in body:
                    return httpx.Response(422, json={"message": "a merged pull request can't be closed or reopened"})
                pull.update({k: v for k, v in body.items() if k in ("state", "body")})
                return httpx.Response(200, json=self._pr_view(repo, pull))
            return httpx.Response(200, json=self._pr_view(repo, pull))
        if method == "POST" and (i := re.fullmatch(r"/issues/(\d+)/comments", rest)):
            self.comments.append((repo, int(i.group(1)), (body or {}).get("body")))
            return httpx.Response(201, json={})
        if method == "GET" and (k := re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", rest)):
            sha, name = k.group(1), params.get("check_name")
            runs = []
            for (r, n), pull in sorted(self.pulls.items()):
                if r == repo and pull["state"] == "open" and self.repos[repo].get(pull["head"]["ref"]) == sha:
                    conclusion = self.checks.get((repo, name), "success")
                    runs.append({"id": 9000 + n, "name": name, "head_sha": sha, "status": "completed" if conclusion else "in_progress",
                                 "conclusion": conclusion, "app": {"id": 15368}, "check_suite": {"id": 5000 + n},
                                 "pull_requests": [{"number": n, "base": {"ref": "main"}}]})
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if method == "GET" and rest == "/actions/runs":
            n = int(params["check_suite_id"]) - 5000
            pull = self.pulls[(repo, n)]
            return httpx.Response(200, json={"workflow_runs": [{"check_suite_id": 5000 + n, "event": "pull_request",
                                                                "head_repository": {"full_name": repo}, "head_branch": pull["head"]["ref"],
                                                                "head_sha": self.repos[repo][pull["head"]["ref"]],
                                                                "pull_requests": [{"number": n, "base": {"ref": "main"}}]}]})
        raise AssertionError(f"RepoWorld has no answer for {method} {path}")


def INTAKE_V2(repo):  # noqa: N802 - a fixture's bytes, as the writer wrote them at the registration
    from portal.writer.render import intake_yaml
    record = {"schema": "intake/v2", "repo": repo, "tier": "moderate", "purpose": "Stores short team notes; no email, no files.",
              "owner": "p_" + "a" * 32, "data_classification": "public", "registered_by": "p_" + "a" * 32, "approved_by": "p_" + "b" * 32,
              "approved_at": "2026-10-23T12:00:00Z", "approval_justification": "Purpose and classification checked.",
              "review_by": "2027-10-23", "request": "r_" + "c" * 32}
    return intake_yaml(record, "r_" + "c" * 32).encode()


def adapter_yaml(adapter_dir, d, namespace, org="acme"):
    """An environment's adapter as golden_road.deploy.adapters writes it (the portal can't import it): the Flux Kustomization names
    ./<d>, the Argo CD Application <d>."""
    from portal.writer.allowlist import adapter_name
    owner, name, env = d.split("/")[1:]
    meta = {"name": adapter_name(owner, name, env)}
    if adapter_dir == "clusters/home/flux":
        doc = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization", "metadata": {**meta, "namespace": "flux-system"},
               "spec": {"suspend": True, "interval": "10m", "path": f"./{d}", "prune": True,
                        "sourceRef": {"kind": "GitRepository", "name": "golden-road-deploy"}, "targetNamespace": namespace}}
    else:
        doc = {"apiVersion": "argoproj.io/v1alpha1", "kind": "Application", "metadata": {**meta, "namespace": "argocd"},
               "spec": {"project": "default", "source": {"repoURL": f"https://github.com/{org}/golden-road-deploy", "targetRevision": "main",
                                                          "path": d},
                        "destination": {"server": "https://kubernetes.default.svc", "namespace": namespace}}}
    return (f"# Generated by golden-road compose; deploy-check fails on any edit. The adapter for {d}.\n"
            + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)).encode()


def deploy_world(world, *, org="acme", repo="acme/notes", environments=("test", "prod"), lifecycle=None, bindings=None, manifests=None,
                 image="ghcr.io/acme/notes@sha256:" + "b" * 64, intake=None):  # the fixture manifests' image
    """golden-road-deploy as bump and the writer leave it: each environment's release.yaml, base/manifests.yaml, bindings.yaml (`bindings`:
    {env: bytes}), lifecycle.yaml (`lifecycle`: {env: bytes}), composed.yaml (the portal's compose of them) and kustomization.yaml, and
    its two adapters; and the factory with `repo`'s intake record (INTAKE_V2, or `intake`'s bytes). Returns (deploy repo, factory repo)."""
    from portal.compose import kustomization
    from portal.writer.allowlist import adapter_name
    from portal.writer.run import compose_bytes
    manifests = manifests or (FIXTURES / "deploy/manifests.yaml").read_bytes()
    owner, name = repo.split("/")
    files = {"clusters/home/flux/gitrepository.yaml": b"# the Flux source\n", "apps/.gitkeep": b""}
    for env in environments:
        d = f"apps/{owner}/{name}/{env}"
        release = {"schema": "deploy-release/v1", "repo": repo, "environment": env, "namespace": f"{name.lower()}-{env}",
                   "release": f"{env}-20261029120000-abcdef1", "image": image, "manifests_sha256": hashlib.sha256(manifests).hexdigest(),
                   "gate_record_sha256": "c" * 64}
        files[f"{d}/release.yaml"] = yaml.safe_dump(release, sort_keys=False).encode()
        files[f"{d}/base/manifests.yaml"] = manifests
        files[f"{d}/kustomization.yaml"] = kustomization(f"{name.lower()}-{env}").encode()
        binding = (bindings or {}).get(env)
        life = (lifecycle or {}).get(env)
        if binding is not None:
            files[f"{d}/bindings.yaml"] = binding
        if life is not None:
            files[f"{d}/lifecycle.yaml"] = life
        files[f"{d}/composed.yaml"] = compose_bytes(repo, env, files[f"{d}/release.yaml"], manifests, binding, None, life)
        for adapters in ("clusters/home/flux", "clusters/office/argo"):
            files[f"{adapters}/{adapter_name(owner, name, env)}.yaml"] = adapter_yaml(adapters, d, f"{name.lower()}-{env}", org)
    deploy, factory = f"{org}/golden-road-deploy", f"{org}/scorecard-factory"
    world.add_repo(deploy, files)
    world.add_repo(factory, {f"intake/{repo}.yaml": INTAKE_V2(repo) if intake is None else intake})
    world.add_repo(repo, {"README.md": b"# app\n"})
    return deploy, factory


LIFECYCLE_KEY = None  # one RSA key for every lifecycle App in a run (a fresh key per run: none is committed)


def lifecycle_app(world, app_id=None):
    """The real lifecycle App (writer.config.LifecycleApp) talking to `world`."""
    from portal.writer.config import LifecycleApp
    global LIFECYCLE_KEY
    LIFECYCLE_KEY = LIFECYCLE_KEY or pem(new_rsa_key())
    return LifecycleApp(app_id or world.lifecycle_app_id, LIFECYCLE_KEY, "https://api.github.com", httpx.Client(transport=transport(world)))


LIFECYCLE_REASON = "Critical vulnerability CVE-2026-0001; suspended until patched"


def lifecycle_request(store, people, now, action="suspend", repo="acme/notes", task="SCTASK0010001", claim=True):
    """A lifecycle request by admin, as the web makes it after the task check (the mock's SCTASK0010001 sys_id), claimed by the writer."""
    from portal.ids import new_id
    req = store.create_lifecycle(request_id=new_id("r"), person_id=people["admin"].id, repo=repo, now=now,
                                 final={"action": action, "task": task, "task_sys_id": "5ca1ab1e000040008000000000010001",
                                        "reason": LIFECYCLE_REASON},
                                 note=f"Requested in the golden road portal by Admin: {action} the app.")
    return store.claim_approved(now=now) if claim else req
