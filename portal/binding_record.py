"""The next binding/v2 document, one environment's, from what the app's admin picked on the app's page (spec 2026-10-29 app
bindings; GitOps deploy repo G3: bindings are per environment). Every choice is checked against the directory here, on the server:
the page only offers valid ones, and a hand-made POST gets the same answer. `repo` is the canonical owner/repo the portal stores;
it is recorded as given (deploy-check matches the file's path exactly)."""

import contextlib
import datetime as dt
import re

from . import env_names
from .contracts import validate
from .secretserver import SecretServerError

REMOVABLE = ("azure-identity", "databricks")
ENVIRONMENTS = ("test", "prod")
OPEN_STATES = ("pending", "sent_back", "approved", "writing", "pr_open")  # store.base.OPEN_STATES (a test keeps them equal)
MAX_SECRETS = 20  # binding/v2's secrets maxItems
SECRET_ID_MAX = 2147483647
FIELD_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")  # binding/v2's secret_ref.field_slug
SECRET_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,126}[A-Za-z0-9._-])?")  # binding/v2's secret_ref.secret_name
# The mock's token endpoint does not check a password (it is a mock); the real version uses the person's own token (spec D7)
MOCK_PASSWORD = "<mock>"


def environment_of(request):
    """The environment a binding request is for: its draft's, or test for a request recorded before bindings were per environment
    (builder 0.3.16's one binding was what the test release rendered). None for anything else, which matches no environment."""
    env = (request.draft or {}).get("environment") if isinstance(request.draft, dict) else None
    if env is None and isinstance(request.draft, dict) and request.draft.get("schema") == "binding/v1":
        return "test"
    return env if env in ENVIRONMENTS else None


def environment_state(requests, environment):
    """(current binding or None, the open binding request or None, the latest change if it failed or None) for one environment,
    from a repo's requests newest first. The current binding is the newest merged one for that environment."""
    if environment not in ENVIRONMENTS:
        raise ValueError("not an environment")
    changes = [r for r in requests if r.kind == "binding" and environment_of(r) == environment]
    current = next((r.final for r in changes if r.state == "merged"), None)
    open_one = next((r for r in changes if r.state in OPEN_STATES), None)
    failed = changes[0] if changes and changes[0].state == "failed" else None
    return current, open_one, failed


class BindingRefused(Exception):
    pass


def _tagged(directory, org, repo, principal_id):
    return next((p for p in directory.principals_for(org, repo) if p.id == principal_id), None)


def _databricks(directory, warehouse) -> dict:
    """The Databricks block as the directory has the warehouse now, never as an old binding recorded it."""
    data = {"directory_id": warehouse.id, "host": warehouse.host, "http_path": warehouse.http_path, "verified_by": directory.name}
    data.update({k: v for k, v in (("catalog", warehouse.catalog), ("schema", warehouse.schema)) if v})
    return data


MOCK_SERVER = "mock-secret-server"  # portal.secretserver_mock.MockSecretServer.name


@contextlib.contextmanager
def secret_tokens(secret_server, secret_user):
    """(client, the person's token, the platform account's token), the client closed afterwards. Through the client exactly as a
    real instance is used; in the mock the person is the Secret Server user their verified GitHub link maps to.

    Mock only (spec D7): minting the person's token with the password grant is what the mock allows, because it checks no
    password. A real Secret Server must use the person's own token from their sign-in (Secret Server federated with the same
    identity provider), never one the portal mints, so anything but the mock is refused here."""
    if getattr(secret_server, "name", None) != MOCK_SERVER:
        raise RuntimeError("only the mock Secret Server mints a person's token; a real one must use the person's own (spec D7)")
    with secret_server.client() as client:
        yield client, client.token(secret_user, MOCK_PASSWORD), client.token(secret_server.platform_user, MOCK_PASSWORD)


def _secret(secret_server, secret_user, secrets, choice) -> dict:
    """A new secret reference, checked as the person (they can see it) and as the platform (it is shared with the platform's account
    and the field is one of its template's non-file fields). Only the id, the field and the env name come from the form; the
    name and the folder are Secret Server's."""
    if secret_server is None:
        raise BindingRefused("Secret Server is not set up here.")
    if not isinstance(secret_user, str) or not secret_user:
        raise BindingRefused("Link your GitHub account (verified) first: the portal finds your Secret Server user through it.")
    secret_id, slug, env = (choice.get(k) for k in ("secret_id", "field_slug", "env")) if isinstance(choice, dict) else (None,) * 3
    if type(secret_id) is not int or not 1 <= secret_id <= SECRET_ID_MAX or not isinstance(slug, str) or not FIELD_SLUG.fullmatch(slug):
        raise BindingRefused("Choose a secret and one of its fields.")
    why = env_names.refused(env)
    if why:
        raise BindingRefused(why)
    if any(s["env"] == env for s in secrets):
        raise BindingRefused(f"{env} already holds a secret in this environment: remove it first, or choose another name.")
    if len(secrets) >= MAX_SECRETS:
        raise BindingRefused(f"An environment has at most {MAX_SECRETS} secrets.")
    failure = None
    try:
        with secret_tokens(secret_server, secret_user) as (client, person_token, platform_token):
            summary = client.secret_summary(person_token, secret_id)  # the person's own view: what they can see
            if summary is None:
                raise BindingRefused("You can't see this secret in Secret Server.")
            if not client.shared_with_platform(platform_token, secret_id):
                raise BindingRefused(f"This secret isn't shared with {secret_server.platform_user}: ask security to share it.")
            if slug not in {f.slug for f in client.template_fields(platform_token, summary.template_id)}:
                raise BindingRefused("That is not a field this secret can deliver: choose one of its template's fields (never a file).")
    except SecretServerError:
        failure = "Secret Server could not answer: try again."  # never the error's text: the person sees this
    if failure:
        raise BindingRefused(failure)
    if not SECRET_NAME.fullmatch(summary.name):
        raise BindingRefused("This secret's name can't be recorded in a binding: rename it in Secret Server (letters, digits, "
                             "spaces, . _ -).")
    return {"env": env, "secret_id": summary.id, "secret_name": summary.name, "folder_id": summary.folder_id, "field_slug": slug,
            "verified_by": secret_server.name}


def next_binding(*, current, repo, org, directory, person_id, request_id, now, environment, principal_id=None, warehouse_id=None,
                 remove=None, add_secret=None, remove_secret=None, secret_server=None, secret_user=None) -> dict:
    """Exactly one of principal_id, warehouse_id, remove, add_secret ({"secret_id", "field_slug", "env"}) or remove_secret (an env
    name). `current` is the binding the environment has now (or None); only its identity, connection and secrets carry over.
    secret_server and secret_user (the person's Secret Server user) are needed only for add_secret."""
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        raise BindingRefused("The environment must be test or prod.")
    if sum(x is not None for x in (principal_id, warehouse_id, remove, add_secret, remove_secret)) != 1:
        raise BindingRefused("Choose one thing to change.")
    identity = dict((current or {}).get("azure_identity") or {}) or None
    data = dict((current or {}).get("databricks") or {}) or None
    secrets = [dict(s) for s in ((current or {}).get("secrets") or [])]
    if add_secret is not None:
        secrets.append(_secret(secret_server, secret_user, secrets, add_secret))
    elif remove_secret is not None:
        if not isinstance(remove_secret, str) or not env_names.ENV_NAME.fullmatch(remove_secret) \
                or not any(s["env"] == remove_secret for s in secrets):
            raise BindingRefused("This environment has no secret by that name: there is nothing to remove.")  # never the form's text
        secrets = [s for s in secrets if s["env"] != remove_secret]
    elif principal_id is not None:
        found = _tagged(directory, org, repo, principal_id)
        if found is None:
            raise BindingRefused(f"This principal isn't tagged for {repo}.")
        if identity is None or identity["directory_id"] != found.id or data is None \
                or data["directory_id"] not in {w.id for w in directory.warehouses_for(found.id)}:
            data = None  # a new identity has none of the old one's grants
        else:
            data = _databricks(directory, directory.warehouse(data["directory_id"]))  # kept, so rebuilt like the identity
        identity = {"directory_id": found.id, "tenant_id": directory.tenant_id, "client_id": found.client_id, "verified_by": directory.name}
    elif warehouse_id is not None:
        if identity is None:
            raise BindingRefused("Link an Azure identity first: the Databricks connection signs in as that identity.")
        principal = _tagged(directory, org, repo, identity["directory_id"])
        if principal is None:
            raise BindingRefused(f"The linked principal isn't tagged for {repo} any more: link an Azure identity again.")
        # the identity as the directory has it now, never as the old binding recorded it
        identity = {"directory_id": principal.id, "tenant_id": directory.tenant_id, "client_id": principal.client_id, "verified_by": directory.name}
        found = next((w for w in directory.warehouses_for(identity["directory_id"]) if w.id == warehouse_id), None)
        if found is None:
            raise BindingRefused(f"This warehouse isn't granted to {identity['directory_id']}.")
        data = _databricks(directory, found)
    else:
        if remove not in REMOVABLE:
            raise BindingRefused("Choose what to remove.")
        if (remove == "azure-identity" and identity is None) or (remove == "databricks" and data is None):
            raise BindingRefused("That is not linked: there is nothing to remove.")
        if remove == "databricks" and _tagged(directory, org, repo, identity["directory_id"]) is None:
            raise BindingRefused(f"The linked principal isn't tagged for {repo} any more. Remove the Azure identity instead: "
                                 "that removes the Databricks connection too.")
        data = None
        identity = None if remove == "azure-identity" else identity
    doc = {"schema": "binding/v2", "repo": repo, "environment": environment}
    if identity:
        doc["azure_identity"] = identity
    if data:
        doc["databricks"] = data
    doc.update(secrets=sorted(secrets, key=lambda s: s["env"]), linked_by=person_id, linked_at=now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), request=request_id)
    problems = validate("binding.v2", doc)
    if problems:
        raise BindingRefused(f"The binding is not valid: {problems[0]}")
    return doc
