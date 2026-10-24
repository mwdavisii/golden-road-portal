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

    def authorize(self, url, *, sub, groups, name="Pat Example", email="pat@example.test"):
        """What the browser does at the authorization endpoint: returns the code the provider redirects back with."""
        q = parse_qs(urlsplit(url).query)
        assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"] and q["client_id"] == [self.client_id]
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"challenge": q["code_challenge"][0], "redirect_uri": q["redirect_uri"][0], "nonce": q["nonce"][0],
                            "claims": {"sub": sub, "groups": groups, "name": name, "email": email}}
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
    ("GET", rf"{REPO_PATH}/git/trees/[0-9a-f]{{40}}", "contents", "read"),
    ("PUT", rf"{REPO_PATH}/contents/.*", "contents", "write"),
    ("POST", rf"{REPO_PATH}/git/refs", "contents", "write"),
    ("GET", rf"{REPO_PATH}/(pulls|pulls/\d+|pulls/\d+/files)", "pull_requests", "read"),
    ("POST", rf"{REPO_PATH}/pulls", "pull_requests", "write"),
    ("PUT", rf"{REPO_PATH}/pulls/\d+/merge", "contents", "write"),
    ("GET", rf"{REPO_PATH}/commits/[0-9a-f]{{40}}/check-runs", "checks", "read"),
    ("GET", rf"{REPO_PATH}/actions/runs", "actions", "read"),
    ("GET", rf"{REPO_PATH}/rulesets(/\d+)?", "administration", "read"),
)


class FakeGitHub:
    """A recorded GitHub. Answers are queued per (METHOD, host, path): (status, body), (status, body, headers) or a
    callable(request). Recorded responses live in tests/fixtures/github/, trimmed to the fields the portal reads. The last
    answer repeats. Every call is logged, and a call with no answer fails the test."""

    def __init__(self, api_host="api.github.com", web_host="github.com", permissions=None):
        self.api_host, self.web_host = api_host, web_host
        self.hosts = {api_host, web_host}
        self.routes, self.calls = {}, []
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
            raise AssertionError(f"FakeGitHub has no answer for {request.method} {request.url}")
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if callable(answer):
            return answer(request)
        status, body, *rest = answer
        return httpx.Response(status, json=body, headers=rest[0] if rest else None)

    def called(self, method, path):
        return [c for c in self.calls if c["method"] == method and c["path"] == path]


def approved_request(store, people, now, repo="acme/notes"):
    """A registration by owner, approved by admin with the record the web would build, claimed by the writer (state writing)."""
    from portal.registration import intake_record
    # the portal fixture already seeds a pending registration of acme/notes by owner: use it rather than open a second one
    req = next((r for r in store.requests_by(people["owner"].id) if r.repo == repo and r.state == "pending"), None) or store.create_registration(
        person_id=people["owner"].id, now=now, draft={"schema": "registration-draft/v1", "repo": repo,
                                                      "purpose": "Stores short team notes; no email, no files.",
                                                      "data_classification": "internal", "proposed_tier": "low"})
    evidence = store.evidence_for(req.id, req.submission) or give_evidence(store, req, now=now)
    final, errors = intake_record(req, approver_id=people["admin"].id, tier="moderate", classification="internal", review_by="2027-10-23",
                                  justification="Purpose and classification checked.", now=now, evidence=evidence)
    assert not errors
    store.decide(request_id=req.id, actor_id=people["admin"].id, actor_groups=["golden-road-admins"], approve=True,
                 note=final["approval_justification"], final=final, now=now)
    return store.claim_approved(now=now)


def writer_config(**changes):
    from portal.writer.config import WriterConfig
    base = dict(org="acme", role_groups={"admin": frozenset({"golden-road-admins"})}, app_id=424242)
    return WriterConfig(**{**base, **changes})


def make_settings(**changes):
    from portal.settings import Settings
    base = dict(base_url=BASE, org="acme", github_host="github.com", oidc_issuer=ISSUER, oidc_client_id="portal-client",
                oidc_client_secret="oidc-secret-for-tests",
                role_groups={"admin": frozenset({"golden-road-admins"}), "ops": frozenset({"golden-road-ops"})},
                github_oauth_client_id="Iv1.testclient", github_oauth_client_secret="gh-secret-for-tests", database_url="postgresql://unused")
    return Settings(**{**base, **changes})


def seed(store, people, now):
    """What every portal test starts from: owner linked to GitHub login owner-gh (id 1001), an admin of acme/notes, and a
    pending registration of acme/notes by owner."""
    from portal.store.base import RepoAdmin
    link = store.add_link(person_id=people["owner"].id, github_login="owner-gh", github_user_id=1001, source="admin",
                          actor_id=people["admin"].id, reason="seeded for the tests", now=now)
    store.replace_repo_admins([RepoAdmin("acme/notes", "owner-gh", 1001, now), RepoAdmin("acme/tools", "owner-gh", 1001, now)], observed_at=now)
    request = store.create_registration(person_id=people["owner"].id, now=now, draft={
        "schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes; no email, no files.",
        "data_classification": "internal", "proposed_tier": "low"})
    give_evidence(store, request, now=now)  # the platform's evidence at the floor low/internal, so an admin can approve
    return {"link": link.id, "request": request.id, "owner": people["owner"].id}


# The factory's config/registration.yaml as a dict (registration-rules/v1): what the floor tests and the collector tests use.
RULES = {"schema": "registration-rules/v1", "no_spec": {"tier": "high", "data_classification": "restricted"},
         "classification_tier": {"public": "low", "internal": "low", "restricted": "high", "phi": "high"},
         "raise_to": {"write_roles": "moderate", "secret_env": "moderate", "model_gateway_egress": "moderate",
                      "other_egress": "moderate", "many_dependencies": "moderate"},
         "many_dependencies_over": 50}


# What the read installation is granted (the collector's App): Metadata and Contents, read only. A call needing more gets 403.
READ_PERMISSIONS = {"metadata": "read", "contents": "read"}
KNOWN_FILES = ("builder-spec.json", ".builder.lock", "requirements.txt", "package-lock.json", "config/registration.yaml")


def repo_at(gh, repo, files, *, commit="a" * 40, branch="main", modes=None):
    """Register a repo's default branch, its head at `commit` and its files on `gh`. A file is bytes (a 200 with base64 content), an int
    (that status) or a dict (used as the body). Known paths that `files` leaves out answer 404."""
    gh.on("GET", f"/repos/{repo}", (200, {"default_branch": branch}))
    gh.on("GET", f"/repos/{repo}/git/ref/heads/{branch}", (200, {"object": {"sha": commit}}))
    # the commit's root tree: each root file is a regular file unless `modes` says otherwise (120000 symlink, 160000 submodule)
    root = [{"path": p, "mode": (modes or {}).get(p, "100644"), "type": "blob", "sha": "b" * 40} for p in sorted(files) if "/" not in p]
    gh.on("GET", f"/repos/{repo}/git/trees/{commit}", (200, {"sha": commit, "tree": root, "truncated": False}))
    for path in sorted(set(KNOWN_FILES) | set(files)):
        entry = files.get(path)
        if entry is None:
            answer = (404, {"message": "Not Found"})
        elif isinstance(entry, bytes):
            answer = (200, {"type": "file", "encoding": "base64", "size": len(entry), "content": base64.b64encode(entry).decode()})
        elif isinstance(entry, int):
            answer = (entry, {"message": "status"})
        else:
            answer = (200, entry)
        gh.on("GET", f"/repos/{repo}/contents/{path}", answer)
    return gh


def evidence_doc(repo, commit="e" * 40, **changes):
    """A valid registration-evidence/v1 for `repo` with a spec, no signals and nothing noteworthy."""
    doc = {"schema": "registration-evidence/v1", "repo": repo, "commit": commit, "spec_present": True, "data_classification": "internal",
           "app_type": "python", "resources_tier": "default", "egress": [], "secret_env": [], "write_roles": [], "write_routes": 0,
           "builder_version": "0.3.10", "dependencies": {"count": 3, "lockfile_present": True, "lockfile": "requirements.txt"}, "signals": []}
    return {**doc, **changes}


def give_evidence(store, request, *, floor_tier="low", floor_classification="internal", commit="e" * 40, now=T0, **changes):
    """Record the collector's evidence for `request`'s current submission, as the collector would."""
    return store.add_evidence(request_id=request.id, submission=request.submission, commit=commit,
                              evidence=evidence_doc(request.repo, commit, **changes), floor_tier=floor_tier,
                              floor_classification=floor_classification, rules_commit="f" * 40, now=now)
