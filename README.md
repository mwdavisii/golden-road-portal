# golden-road-portal

The golden road portal. Slice 1 covers sign-in with the identity provider (OIDC), people and identity links, an append-only audit log, a public Start here page, and app registration: a person submits a registration (a draft can arrive in the URL fragment from `golden-road register`), an admin approves it, and a separate writer process records it in the scorecard factory as an `intake/v2` file through a GitHub App.

This app is packaged by the builder (`builder-spec.json`, app type `python`). Don't edit the generated files (`Dockerfile`, `.dockerignore`, `deploy/base/`, `_platform/`, `.builder.lock`, the golden-road blocks in `AGENTS.md` and `CLAUDE.md`): change `builder-spec.json` and run `builder generate .`.

## Processes

One image, three entry points, plus the migration runner:

| Process | Command | Credentials |
|---|---|---|
| web | `portal.main:app` (the generated `_platform/entry.py` wraps it) | database as `portal_web`; OIDC client secret; the read App's OAuth client secret |
| writer | `python -m portal.writer` | database as `portal_writer`; the write App's key |
| collector | `python -m portal.collectors` | database as `portal_collector`; the read App's key |
| migrations | `python -m portal.migrate` | `PORTAL_MIGRATE_DATABASE_URL` as `portal_owner` |

Slice 1 generates only the web Deployment. The writer and collector Deployments, with their own credentials, come with the proof-of-concept wiring slice, so the web pod never holds the write key.

## Unconfigured mode

The platform's preview runs the image with no environment and then the authz probe. So when any required web setting is missing or invalid (a partial configuration counts), `portal.main` builds a small app instead of stopping: `/healthz` answers 200 with a body that says the portal is not configured and lists the setting names (never values); every other path and method answers 503 with one fixed text; there is no sign-in, database connection, store, form or state change. It logs one startup line naming the settings. The generated `_platform` layer still wraps it. The writer, collector and migrate entry points are unchanged: they exit non-zero on a missing setting.

Trade-off: an unconfigured pod reports healthy (Kubernetes probes and the preview pass) but serves only 503. After a deploy, confirm `/healthz` does not say "not configured".

## Database roles

The database administrator creates `portal_owner` (owns the database and runs migrations), `portal_web`, `portal_writer` and `portal_collector`, each with LOGIN, before the first migration. The migration grants each role only its process's work: the audit log is insert and read only for `portal_web` and `portal_writer`, and a trigger refuses any change to it, even by the owner. Each process gets its own `PORTAL_DATABASE_URL`.

## Authorization (a recorded deviation, P14)

The portal signs people in with OIDC and enforces its own roles (contributor, ops, admin) from the identity provider's groups claim, mapped by `PORTAL_ROLE_GROUPS`. It ignores `X-Platform-Roles` and any other identity header. The generated `_platform` layer has one role, `portal`, allowed on every route, so the edge must send `<identity header>: portal` on every request, including anonymous Start here visits. The role-matrix test (`tests/test_portal_role_matrix.py`) stands in for the authz probe: it covers every route for every kind of caller.

## Configuration

| Variable | Meaning |
|---|---|
| `PORTAL_BASE_URL` | The portal's own origin: https, no path (the session cookie is a `__Host-` cookie) |
| `PORTAL_ORG` | The GitHub organization that holds the platform repos |
| `PORTAL_GITHUB_HOST` | The GitHub host, for example `github.com` |
| `PORTAL_OIDC_ISSUER`, `PORTAL_OIDC_CLIENT_ID`, `PORTAL_OIDC_CLIENT_SECRET` | The identity provider client |
| `PORTAL_ROLE_GROUPS` | JSON: `{"admin": [groups], "ops": [groups]}`; `admin` is required |
| `PORTAL_GITHUB_OAUTH_CLIENT_ID`, `PORTAL_GITHUB_OAUTH_CLIENT_SECRET` | The read App's OAuth client, for linking a GitHub login |
| `PORTAL_DATABASE_URL` | The process's own database URL |
| `PORTAL_OIDC_SCOPES`, `PORTAL_OIDC_GROUPS_CLAIM`, `PORTAL_OIDC_ALGORITHMS` | Optional; default `openid profile email`, `groups`, `RS256` |
| `PORTAL_REVIEW_MONTHS` | Optional; months until a registration's review date (1 to 36, default 12) |
| `PORTAL_REPO_ADMINS_STALE_AFTER` | Optional; seconds after which the collector's observation of a person's admin repos is too old to register or approve on (60 to 604800, default 600: two collector intervals) |

Anything missing or malformed stops the process at startup.

## The writer

`python -m portal.writer [--once]` reads these from its own environment (the web never gets them):

| Variable | Meaning |
|---|---|
| `PORTAL_GITHUB_HOST`, `PORTAL_ORG`, `PORTAL_ROLE_GROUPS`, `PORTAL_DATABASE_URL` | As for the web; the database URL is the `portal_writer` role's |
| `PORTAL_WRITE_APP_ID`, `PORTAL_WRITE_INSTALLATION_ID`, `PORTAL_WRITE_APP_KEY_FILE` | The write App installation and its RSA private key (a PEM file, never printed) |
| `PORTAL_WRITER_INTERVAL` | Optional; seconds between passes (5 to 3600, default 30) |
| `PORTAL_WRITER_REQUIRED_CHECKS` | Optional; comma-separated checks that must pass before a merge. `factory-check` is always required; others can be added |

It checks the write App's bypass before the first pass and again before every pass, and writes nothing in a pass whose check finds a
problem. Exit codes: 2 for a missing or bad setting or key, 3 when the bypass check refuses to start it.
It merges only the commit it opened the pull request with (migration 0003 stores it), and only while the pull request still adds
exactly the one allowlisted file on the default branch.

The writer's GitHub surface is exactly these calls (the write installation needs `contents` and `pull_requests` write, and `metadata`, `checks`, `administration` and `actions` read: `actions: read` is for the workflow-run call, which a private repo refuses without it). As the App (a short JWT): `POST /app/installations/{id}/access_tokens`, `GET /app/installations/{id}`.
As the write installation: `GET /installation/repositories`, `GET /repos/{r}`, `GET /repos/{r}/git/ref/heads/{branch}`, `POST /repos/{r}/git/refs`,
`GET` and `PUT /repos/{r}/contents/{path}` (`GET` at a branch, at a commit, and at the merge commit after a merge), `GET /repos/{r}/commits/{sha}`,
`GET /repos/{r}/pulls?head=...`, `POST /repos/{r}/pulls`, `GET /repos/{r}/pulls/{n}`, `GET /repos/{r}/pulls/{n}/files`,
`GET /repos/{r}/commits/{sha}/check-runs`, `GET /repos/{r}/actions/runs?check_suite_id=...` (to trace a check run to its workflow run),
`PUT /repos/{r}/pulls/{n}/merge`, and `GET /repos/{r}/rulesets` and `/rulesets/{id}` (with `includes_parents=true`).
The check that lets a merge through must be a `pull_request` workflow run for the writer's branch and commit, from the target repo,
that lists only the writer's PR into the default branch. If another PR shares the head, the REST API doesn't say which of them
triggered a run, so the writer doesn't decide: it waits, and fails the request at `merge_timeout`. After a merge it reads the file at
the merge commit; if GitHub can't say yet (an error, or a 404 within 30 minutes of the merge) it keeps the request open as "merge confirmation pending" and tries again next pass. Once a PR is merged the writer never fails the request for want of an answer: it fails only on other content, or on a definite 404 after that grace period.

Migrations: `0001` the tables and grants, `0002` the database's own rules (the request state machine, no self-approval, the audit
log's id and time), `0003` the pinned commits and no admin link to one's own person, `0004` the platform's registration evidence and
`0005` the approval rule that uses it.

Registration evidence: the collector inserts into and reads `registration_evidence` (one evidence row per submission, and the reasons a
request waits), and reads only the id, kind, repo, state and submission of `request`, never the draft. Nobody updates or deletes
evidence, not even the schema owner (a trigger refuses it). A resubmission counts `request.submission` up, so it gets fresh
evidence. `0005` makes the database refuse an approval without the evidence of the request's current submission, and one below the
stored floor unless the approver recorded an override reason of at least 30 characters. The collector needs `PORTAL_ORG` and
`PORTAL_MODEL_GATEWAY_HOSTS` (comma-separated host names).

## Development

```bash
uv pip install --python .venv/bin/python --require-hashes -r bootstrap/golden-road-portal/requirements-test.txt
PYTHONPATH=src .venv/bin/pytest bootstrap/golden-road-portal/tests -q
PYTHONPATH=src .venv/bin/pytest -m docker bootstrap/golden-road-portal/tests -q   # Postgres: needs docker
```

`requirements.txt` and `requirements-test.txt` are locked with `uv pip compile --generate-hashes --python-version 3.12` (see the header of `requirements.in`). The portal vendors six schemas into `portal/schemas/`; a repo-level test keeps them identical to the platform's contracts.
