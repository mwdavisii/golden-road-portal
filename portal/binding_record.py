"""The next binding/v1 document from what the app's admin picked on the app's page (spec 2026-10-29 app bindings). Every choice is
checked against the directory here, on the server: the page only offers valid ones, and a hand-made POST gets the same answer.
`repo` is the canonical owner/repo the portal stores; it is recorded as given (the release matches the file's path exactly)."""

import datetime as dt

from .contracts import validate

REMOVABLE = ("azure-identity", "databricks")


class BindingRefused(Exception):
    pass


def _tagged(directory, org, repo, principal_id):
    return next((p for p in directory.principals_for(org, repo) if p.id == principal_id), None)


def _databricks(directory, warehouse) -> dict:
    """The Databricks block as the directory has the warehouse now, never as an old binding recorded it."""
    data = {"directory_id": warehouse.id, "host": warehouse.host, "http_path": warehouse.http_path, "verified_by": directory.name}
    data.update({k: v for k, v in (("catalog", warehouse.catalog), ("schema", warehouse.schema)) if v})
    return data


def next_binding(*, current, repo, org, directory, person_id, request_id, now, principal_id=None, warehouse_id=None, remove=None) -> dict:
    """Exactly one of principal_id, warehouse_id or remove. `current` is the binding the app has now (or None)."""
    if sum(x is not None for x in (principal_id, warehouse_id, remove)) != 1:
        raise BindingRefused("Choose one thing to change.")
    identity = dict((current or {}).get("azure_identity") or {}) or None
    data = dict((current or {}).get("databricks") or {}) or None
    if principal_id is not None:
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
    doc = {"schema": "binding/v1", "repo": repo}
    if identity:
        doc["azure_identity"] = identity
    if data:
        doc["databricks"] = data
    doc.update(linked_by=person_id, linked_at=now.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), request=request_id)
    problems = validate("binding.v1", doc)
    if problems:
        raise BindingRefused(f"The binding is not valid: {problems[0]}")
    return doc
