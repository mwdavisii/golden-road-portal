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
| `PORTAL_DIRECTORY` | Optional; `mock` turns on each app's connections (an Azure identity and a Databricks connection, picked from `portal/mock_directory.json`); unset, the app page shows none. The writer must get the same value |
| `PORTAL_IDENTITY_PROCESS_URL`, `PORTAL_DATA_PROCESS_URL` | Optional; `https://` pages that say how to ask for a service principal and for a warehouse grant (linked on the app page) |
| `PORTAL_SECRET_SERVER` | Optional; `mock` turns on each app's secrets from Delinea Secret Server (`portal/mock_secret_server.json`, served through `portal/secretserver.py`'s client, which never reads a value); unset, the app page shows none. The writer must get the same value |
| `PORTAL_SECRET_PROCESS_URL` | Optional; an `https://` page that says how to ask security to share a secret with the platform account `golden-road-eso` (linked on the app page) |
| `PORTAL_CLASSIFICATION_POLICY_URL` | Optional; an `https://` page with the data classification policy, linked (new tab) on the register, resubmit and approval forms. Empty or unset hides the link. |
| `PORTAL_REREVIEW_STALE_DAYS` | Optional; days after which an open re-review item is flagged stale on the Re-review page (0 to 36500, default 30; `0` turns the flag off) |
| `PORTAL_CLOUDFLARE` | Optional; `mock` turns on each app's MCP servers from the shared Cloudflare MCP gateways (`portal/mock_cloudflare.json`); unset, the app page shows none. The writer must get the same value |
| `PORTAL_MCP_GATEWAY_TEST`, `PORTAL_MCP_GATEWAY_PROD` | Optional; each environment's gateway host, the same as `platform.json`'s `mcp_gateways` (`configure.py --mcp-gateway-test`, `--mcp-gateway-prod`). Empty is the placeholder, `mcp-test.example.com` or `mcp.example.com`; the two must differ. The writer must get the same values |
| `PORTAL_MCP_PROCESS_URL` | Optional; an `https://` page that says how to ask for a server to be added to a gateway (linked on the app page) |
| `PORTAL_SKILLS_CATALOG` | Optional; `mock` turns on each app's skills from the platform's catalog (`portal/mock_skills_catalog.json`); unset, the app page shows none. The writer must get the same value |
| `PORTAL_SERVICENOW` | Optional; `mock` shows the collector's CMDB records (the app page's card, the request's Registered in ServiceNow step). The collector reads it too |
| `PORTAL_ORG_NAME` | Optional; the organization's name in the ownership notice (one line, 1 to 100 characters; empty is "the organization") |
| `PORTAL_LIFECYCLE_GRACE_DAYS` | Optional; the days the ownership notice gives (0 to 36500, default 30; `0` leaves the day count out, it never means "no time") |
| `PORTAL_CLOUDFLARE_ACCOUNT_ID`, `PORTAL_SERVICENOW_URL` | Must stay unset: they are for a real Cloudflare account and a real ServiceNow instance, which there are none of yet. Set, each process that reads them stops (the web both, the writer the account id, the collector the URL) |
| `PORTAL_SECRET_SERVER_URL` | Must stay unset: it is for a real Secret Server, which there is none of yet. Set, the portal stops (web and writer) |

Anything missing or malformed stops the process at startup.

## The writer

`python -m portal.writer [--once]` reads these from its own environment (the web never gets them):

| Variable | Meaning |
|---|---|
| `PORTAL_GITHUB_HOST`, `PORTAL_ORG`, `PORTAL_ROLE_GROUPS`, `PORTAL_DATABASE_URL` | As for the web; the database URL is the `portal_writer` role's |
| `PORTAL_WRITE_APP_ID`, `PORTAL_WRITE_INSTALLATION_ID`, `PORTAL_WRITE_APP_KEY_FILE` | The write App installation and its RSA private key (a PEM file, never printed) |
| `PORTAL_WRITER_INTERVAL` | Optional; seconds between passes (5 to 3600, default 30) |
| `PORTAL_WRITER_REQUIRED_CHECKS` | Optional; comma-separated checks that must pass before a merge in the factory. `factory-check` is always required; others can be added. In `golden-road-deploy` the checks are `PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS` |
| `PORTAL_WRITER_DEPLOY_REQUIRED_CHECKS` | Optional; the same for `golden-road-deploy`: `deploy-check` is always required; others can be added |
| `PORTAL_DIRECTORY` | Optional; as for the web (`mock` or unset). Every binding is re-checked against it before it is written; unset, bindings fail with `no_directory` |
| `PORTAL_SECRET_SERVER` | Optional; as for the web (`mock` or unset; `PORTAL_SECRET_SERVER_URL` must stay unset). A binding's secrets are re-checked against it before they are written |
| `PORTAL_CLOUDFLARE`, `PORTAL_MCP_GATEWAY_TEST`, `PORTAL_MCP_GATEWAY_PROD` | Optional; as for the web. A binding's MCP servers are re-checked against them before they are written |
| `PORTAL_SKILLS_CATALOG` | Optional; as for the web. A binding's skills are re-checked against it, and the skills ConfigMap is built from it |
| `PORTAL_REPO_ADMINS_STALE_AFTER` | Optional; the web's setting, and it must be the same value (the homelab kit passes both processes the one config map key; empty is the default, 600). A binding's requester must have been seen as the repo's admin within it, or it fails with `requester_not_admin` |

It checks the write App's bypass before the first pass and again before every pass, and writes nothing in a pass whose check finds a
problem. Exit codes: 2 for a missing or bad setting or key, 3 when the bypass check refuses to start it.
It merges only the commit it opened the pull request with (migration 0003 stores it), and only while the pull request still changes
exactly the allowlisted files on the default branch, each written by one of its own commits on the base it branched from. A
registration adds `intake/<owner>/<repo>.yaml` in `<org>/scorecard-factory`. A binding (one environment's, `test` or `prod`)
writes `apps/<owner>/<repo>/<env>/bindings.yaml` and then `apps/<owner>/<repo>/<env>/composed.yaml` in `<org>/golden-road-deploy`,
two contents `PUT`s, each naming the blob it replaces when the file exists (an unlink rewrites `bindings.yaml`, never deletes it).
`composed.yaml` is computed with `portal/compose.py`, a byte-identical copy of `golden_road/compose.py`, from the environment's
`release.yaml` and `base/manifests.yaml` at the base commit, so `deploy-check`'s drift check passes; it is not written when it would
not change. The writer never writes `release.yaml`, `kustomization.yaml`, `base/` or the adapters under `clusters/` (those come
from the deploy repo's `bump.yml`, and none of them depends on a binding). A binding fails with `not_registered` when the
factory has no intake record for the repo at its default branch, `not_deployed` when the environment has no `release.yaml` or
`base/manifests.yaml` yet (no release is pinned there), and `compose_refused` when `release.yaml` is not a valid
`deploy-release/v1` for that repo and environment, the manifests are not its `manifests_sha256`, or compose refuses them.
A binding's Delinea secrets are re-checked as the platform account `golden-road-eso` before anything is written: it fails with
`secret_not_shared` (the account can't see the secret now), `secret_field_gone` (the field is not a non-file field of the secret's
template now), `secret_env_reserved` (the env name is the platform's), `no_secret_server` (`PORTAL_SECRET_SERVER` is unset or not the mock), `secret_verified_elsewhere` (the reference was checked
by another Secret Server than the writer's) or `secret_server_unavailable`. An env name the app's own `builder-spec.json`
sets is `compose_refused`. The person's own visibility is checked by the web on submit, not here: the writer has no token of theirs.
Mock only: the only Secret Server is the mock; no Secret Server, External Secrets Operator or cluster is ever called.

The writer's GitHub surface is exactly these calls (the write installation needs `contents` and `pull_requests` write, and `metadata`, `checks`, `administration` and `actions` read: `actions: read` is for the workflow-run call, which a private repo refuses without it). As the App (a short JWT): `POST /app/installations/{id}/access_tokens`, `GET /app/installations/{id}`.
As the write installation: `GET /installation/repositories`, `GET /repos/{r}`, `GET /repos/{r}/git/ref/heads/{branch}`, `POST /repos/{r}/git/refs`,
`GET` and `PUT /repos/{r}/contents/{path}` (`GET` at a branch, at a commit, and at the merge commit after a merge), `GET /repos/{r}/commits/{sha}`,
`GET /repos/{r}/pulls?head=...`, `POST /repos/{r}/pulls`, `GET /repos/{r}/pulls/{n}`, `GET /repos/{r}/pulls/{n}/files`,
`PATCH /repos/{r}/pulls/{n}` (an adopted PR's body, and closing a failed request's PR) and `POST /repos/{r}/issues/{n}/comments`
(the failure reason on that PR; both best effort),
`GET /repos/{r}/commits/{sha}/check-runs`, `GET /repos/{r}/actions/runs?check_suite_id=...` (to trace a check run to its workflow run),
`PUT /repos/{r}/pulls/{n}/merge`, and `GET /repos/{r}/rulesets` and `/rulesets/{id}` (with `includes_parents=true`).
Both target repos' `main` rulesets require a pull request to be up to date with the default branch, and the writer never updates
its branch. So when the default branch has moved past the commit the writer branched from (or GitHub reports the PR `behind`),
the request fails at once with `base_moved`: the repo changed while the change was waiting; make the change again. Any failed
request's open PR gets the reason as a comment and is closed: the writer never merges it. The writer never merges a prod binding
at all: it opens and checks the PR, a person merges it after `main-review`, and the writer records the merge, with no timeout.
The check that lets a merge through must be a `pull_request` workflow run for the writer's branch and commit, from the target repo,
that lists only the writer's PR into the default branch. If another PR shares the head, the REST API doesn't say which of them
triggered a run, so the writer doesn't decide: it waits, and fails the request at `merge_timeout`. After a merge it reads the file at
the merge commit (a binding's two files: `bindings.yaml` as written, and `composed.yaml` as it reads at the commit the pull request
was opened with); if GitHub can't say yet (an error, or a 404 within 30 minutes of the merge) it keeps the request open as "merge confirmation pending" and tries again next pass. Once a PR is merged the writer never fails the request for want of an answer: it fails only on other content, or on a definite 404 after that grace period.

Migrations: `0001` the tables and grants, `0002` the database's own rules (the request state machine, no self-approval, the audit
log's id and time), `0003` the pinned commits and no admin link to one's own person, `0004` the platform's registration evidence and
`0005` the approval rule that uses it, `0006` binding requests (the one kind that starts approved, with no decision), `0007`
bindings per environment (one open request per kind, repo and environment; the insert guard takes `binding/v2` for `test` or
`prod` only; a binding request recorded before it, a `binding/v1` with no environment, counts as `test`'s and is left as it is),
`0008` the three classification levels, `0009` the audit log's subject index, and `0010` registration changes and the re-review list
(below): the record a change replaces on its evidence, the collector's caches (`intake_observed`, `rereview_run`, `rereview_event`,
`rereview_failed_run`, `rereview_watermark`, `rereview_source`) and the re-review items. Only `portal_collector` writes the caches
and opens items; `portal_web` may only close an item, never for an app the closer owns; the collector's audit entries are `system`
ones about items only, each written once. `0011` is the CMDB record and the ownership acknowledgement (below).

Migrations are forward-only: there are no down migrations. After `0006` an older image still handles registrations, but its
writer fails every binding request it claims (it knows only registrations). Don't roll the image back once bindings exist; if you
must, expect open binding requests to fail, and link them again after rolling forward. After `0007` an open `binding/v1` request
fails the writer's recheck (`schema_invalid`: the writer writes `binding/v2` only); link it again on the app page's test tab.

Registration evidence: the collector inserts into and reads `registration_evidence` (one evidence row per submission, and the reasons a
request waits), and reads only the id, kind, repo, state and submission of `request`, never the draft. Nobody updates or deletes
evidence, not even the schema owner (a trigger refuses it). A resubmission counts `request.submission` up, so it gets fresh
evidence. `0005` makes the database refuse an approval without the evidence of the request's current submission, and one below the
stored floor unless the approver recorded an override reason of at least 30 characters. The collector needs `PORTAL_ORG` and
`PORTAL_MODEL_GATEWAY_HOSTS` (comma-separated host names), and reads three optional re-review settings: `PORTAL_REVIEW_DUE_SOON_DAYS`
(days before a review date when it is due soon; empty is 30, `0` turns the reason off), `PORTAL_REREVIEW_SIGNALS` (the scorecard
signals that put an app on the list: trigger IDs, the recorded signals `builder-bumped`, `egress-removed`, `resources-lowered`, and
`security_review` for any trigger at that level; empty is `security_review`, `T-17`, `T-18`, `T-19`, `egress-removed`) and
`PORTAL_ARTIFACT_HOSTS` (the storage hosts an artifact download may be redirected to, names or `*.suffix`; empty is
`*.blob.core.windows.net`). A bad value stops the collector with the setting's name, never its value.

The factory's YAML (the floor rules and the intake records) is read with the scorecard's own rules, including one added in 0.5.1:
a file with YAML anchors or aliases is refused before it is built (a few hundred bytes of aliases expand past anything a walk over
the document finishes). The scorecard refuses the same, so factory-check and the poller agree.

## Changing a registration

A registration request for a repo that already has an intake record in the factory is a change: the same form, evidence, floor and
approval as a first registration. The register page, opened as `/register?repo=<owner>/<repo>` for a repo the person administers, is
titled "Change or renew the registration" and pre-filled from the record. For every waiting registration the collector also reads
the factory's `intake/<owner>/<repo>.yaml` at the rules commit and stores its blob SHA and a small view of it with the evidence
(`record_sha`, `record`); a record it can't compare makes the request wait (`record_unreadable`), and so does a record the factory
holds under two spellings of its name (`record_case_mismatch`). The approval screen and the request page show current → proposed,
an owner change ("Owner changes from A to B") and a renewal (the same purpose and classification).

The writer replaces the record only if its blob SHA at the branch base is the stored one (otherwise `record_changed`; GitHub
refusing the write as a conflict is `record_conflict`; a record's path that is a directory, a symlink or a submodule is `not_a_file`;
a first registration that meets a record still fails `already_registered`).
The new record is a complete `intake/v2` from the approval; it keeps the current record's `scorecard: central` (the recheck reports
`scorecard_changed` if it doesn't), is committed as `modified`, and the pull request is titled "Change the registration of ..." and
lists each changed field.

## The re-review list

`/admin/rereview` (admins) lists open items, one app and one reason each: `review_overdue`, `review_due_soon` (from each intake/v2
record's `review_by`) and `scorecard_signal` (from the scorecard's re-review events). The collector opens date items and closes one by
itself when a change or renewal moves the date; an admin closes any item with a note (10 to 1000 characters, one line, audited), never
one for an app they own, and the close names the events they saw (a later one sends the page back). A closed item never reopens: a
later reason opens a new one. An item open longer than `PORTAL_REREVIEW_STALE_DAYS` is flagged stale. An intake/v1 record has no
review date, so it is listed as registered outside the portal, and records the portal can't read are listed with why.

Each pass the collector reads, through the read App (which needs `actions: read` for the events; without it the page shows
`actions_unreadable`):

- the factory's intake records at its default branch: `GET /repos/{r}/git/trees/{sha}` and `GET /repos/{r}/git/blobs/{sha}`;
- the factory's `scorecard-assess.yml` runs and each per-app repo's `scorecard.yml` runs: `GET /repos/{r}/actions/workflows/{workflow}/runs`,
  down to the source's watermark (every run above it is done), and `GET /repos/{r}/actions/runs/{id}` to re-check a run that had
  failed until a later attempt succeeds or its artifacts are gone;
- each new run's `scorecard` artifact: `GET /repos/{r}/actions/runs/{id}/artifacts?name=scorecard`, then
  `GET /repos/{r}/actions/artifacts/{id}/zip`, a 302 to Actions' storage, which must be one of `PORTAL_ARTIFACT_HOSTS` and is
  fetched without the token, as stored bytes, never logged; a redirect anywhere else records nothing and shows
  `artifact_host_refused` on the page until the setting (or the egress) is fixed. The zip (at most 16 MiB) is read in memory and only its entry
  `re-review-event.json` (at most 1 MiB) is read, then validated against `re-review-event/v1`; nothing is extracted.

Runs are listed whole (GitHub's filtered list stops at 1000) and judged here. A run is read only when GitHub says it is the factory's
own dispatched assessment of that head, from its default branch `main` with a head on that branch (the branch resolved by
`git/ref/heads/main`, then `GET /repos/{r}/compare/{commit}...{head}`: a tag named `main` reports the same branch name), titled as the
workflow's `run-name` titles it, or the app's own pull request run calling the factory's reusable workflow from
`main`; anything else is counted as `untrusted_run`, never read. In per-app mode the pull request's author could still overwrite the
artifact after the reusable workflow wrote it: an accepted risk, which central mode doesn't have. Events are kept once per source
(central or per-app), so a per-app event never hides the central one. An event whose app isn't registered yet waits and is read again
each pass; a run still going after a day, or one that failed, is re-checked by id. Every run is recorded once with its outcome, and
anything that fails is counted on the page, never shown. The page also shows each collector part's last good pass (flagged after three
missed intervals), each run source's watermark and what waits.

## MCP servers, skills and the CMDB record (0.6.0, mock)

Mock only: nothing here calls Cloudflare, ServiceNow or a skills repo. Each client is written against the real API and served by an
`httpx.MockTransport` over a fixture.

**MCP servers.** With `PORTAL_CLOUDFLARE=mock` the app page has an MCP servers section per environment. Each environment has one
shared Cloudflare MCP gateway (`PORTAL_MCP_GATEWAY_TEST`, `PORTAL_MCP_GATEWAY_PROD`). `portal/cloudflare.py` makes one request,
`GET /accounts/{id}/access/ai-controls/mcp/portals?search=<hostname>&page=<n>&per_page=100`, with an API token that has MCP Portals
Read, and pages until `result_info.total_pages`. A gateway is matched by its exact hostname; a server is offered only when its
status is `ready`. A server with `on_behalf` (each person's own sign-in) is shown greyed: an app can't use it. Which servers a
person may use is not in Cloudflare's list: the per-user policy is the mock's (`mock_cloudflare.json`'s `users`, keyed by the
person's sign-in email). The real version must evaluate the person against each server's Access policies; that is a recorded gap.
A sign-in with no email sees no servers. The portal keeps a sign-in's email only when the ID token says `email_verified: true`, so
a self-asserted email decides neither the MCP servers a person may pick nor the CMDB owner. The steps are Environment, Server, Review; the submit re-checks each one, at most 20
servers per environment. Remove asks first.

**Skills.** With `PORTAL_SKILLS_CATALOG=mock` the app page has a skills section. The catalog is `portal/mock_skills_catalog.json`,
byte for byte the fixture `golden-road deploy-verify` reads (`src/golden_road/data/mock_skills_catalog.json`), so the writer and
deploy-check build the same skills ConfigMap. A person may pick a skill whose `skill.yaml` lists the environment and whose audience
is `everyone` or one of their sign-in groups. A skill is pinned to the catalog's `main` commit; Update moves it to the current one
(a new request) and Remove asks first. At most 20 skills, 64 files and 256 KiB per skill, and 512 KiB per environment.

**What a binding generates.** In `golden-road-deploy`, for an environment with MCP servers: `MCP_URL`, `MCP_SERVERS`, egress to
the gateway on 443, and `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` in the environment's `ExternalSecret`, read from
Secret Server at `/golden-road/mcp-tokens/cf-mcp-<owner>--<repo>-<env>`. For skills: the `<app>-skills` ConfigMap, mounted
read-only at `/skills`, and `SKILLS_DIR=/skills`. And a third file beside `composed.yaml`, `cloudflare-access.yaml`: the desired
state a platform job would reconcile into Cloudflare (the token and the servers whose policy names it). It is never applied, and it
stays with `servers: []` after the last server is removed.

**The writer.** A binding's pull request writes `bindings.yaml`, then `composed.yaml` if it changes, then `cloudflare-access.yaml`
if it changes; it never deletes one. Before it writes, it re-checks the MCP servers (`mcp_owner_invalid`, `mcp_gateway_mismatch`,
`no_cloudflare`, `mcp_verified_elsewhere`, `cloudflare_unavailable`, `mcp_gateway_gone`, `mcp_server_gone`, `mcp_server_not_ready`,
`mcp_server_user_auth`) and the skills (`no_skills_catalog`, `skill_verified_elsewhere`, `skill_gone`, `skill_environment`,
`skill_too_large`). A binding without `mcp` whose base has a `cloudflare-access.yaml` fails `access_file_without_mcp`. The
person's own view (the per-user policy, a skill's audience) can't be re-checked here: the writer has no person, so the web checks
it on submit.

**The CMDB record.** With `PORTAL_SERVICENOW=mock` the collector has a fifth part, after intake. Each pass it sends each intake
record that is new, changed since its last good sync, or whose last attempt failed, to ServiceNow as a `cmdb_ci_business_app`,
identified by `correlation_id` (`github:<owner>/<repo>`, lower case). `portal/servicenow.py` makes three requests:
`GET /api/now/table/sys_user` (the owner's user, by email: exactly one, or `cmdb_owner_unknown`),
`POST /api/now/identifyreconcile?sysparm_data_source=golden_road` (create or update the CI) and
`GET /api/now/table/cmdb_ci_business_app/<sys_id>` (its number, `APM0001001` and on in the mock). An `intake/v2` record whose
owner is unknown is not sent: a CI with no owner is an orphaned app. An `intake/v1` record is sent without `owned_by`. The
purpose is cut at 1000 characters for `short_description`. The problems are `cmdb_unreachable` (ServiceNow didn't answer; the
rest wait for the next pass), `cmdb_owner_unknown` and `cmdb_refused` (an item came back with errors), each shown on the
Re-review page with the repos whose last attempt failed. The log line is `collectors: cmdb: N synced, M unchanged`. Nothing
changes the owner in ServiceNow from the portal, and nothing retires a CI.

**What owning the app means.** One template, `_ownership.html`: patching, validation, leaving, with `PORTAL_ORG_NAME` and
`PORTAL_LIFECYCLE_GRACE_DAYS`. The register form shows it with a required checkbox, "I understand what owning this app means"; a
submission without it is refused and nothing is stored. The approval screen shows when it was acknowledged ("Not recorded" for a
request from before 0.6.0). Once the collector has a CI number, the app page shows it as a card, and the request's timeline gains
the step Registered in ServiceNow. Mock only: nothing suspends, asks, tasks or decommissions.

Migration `0011` adds `cmdb_record` (repo, `sys_id`, number, the synced fields' hash, the last good sync and the last error; only
`portal_collector` writes it, the web reads it), `request.ownership_acknowledged_at` (NULL for every request before it; registrations
only; never changed after the insert), the `cmdb` part on `rereview_source`, and the collector's read of a person's email. It is
forward-only, as `0010` is.

## Development

```bash
uv pip install --python .venv/bin/python --require-hashes -r bootstrap/golden-road-portal/requirements-test.txt
PYTHONPATH=src .venv/bin/pytest bootstrap/golden-road-portal/tests -q
PYTHONPATH=src .venv/bin/pytest -m docker bootstrap/golden-road-portal/tests -q   # Postgres: needs docker
```

`requirements.txt` and `requirements-test.txt` are locked with `uv pip compile --generate-hashes --python-version 3.12` (see the header of `requirements.in`). The portal vendors six schemas into `portal/schemas/`; a repo-level test keeps them identical to the platform's contracts.
