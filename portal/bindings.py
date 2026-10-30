"""What an environment's binding (binding/v2, in golden-road-deploy's apps/<owner>/<repo>/<env>/bindings.yaml) adds to the
container: its Azure workload identity's client id, plain environment variables, the hosts it must reach on 443, and its Delinea
Secret Server references (an id and a field, never a value), which compose turns into one ExternalSecret.
golden-road compose applies it to the release's signed manifests to make that environment's composed.yaml; the portal shows the
same thing on the app's page and its writer composes with the same code. The two packages hold this file byte for byte (a repo
test keeps them equal), so they can't disagree. The input is a document that already validated (azure_identity and databricks
are the same in binding/v1 and binding/v2)."""

AZURE_LOGIN = "login.microsoftonline.com"  # where the workload identity's token is exchanged


def effects(binding) -> dict:
    """{"client_id", "env": [(name, value)], "egress": [host], "secrets": [(env, secret_id, field_slug)]} for a binding. The
    identity's env and egress need an Azure identity, because the Databricks connection authenticates as it; secrets don't, and
    are sorted by env name. A fresh dict every call, so no caller can change another's."""
    secrets = sorted((s["env"], s["secret_id"], s["field_slug"]) for s in ((binding or {}).get("secrets") or []))
    identity = (binding or {}).get("azure_identity")
    if not identity:
        return {"client_id": None, "env": [], "egress": [], "secrets": secrets}
    env = [("AZURE_CLIENT_ID", identity["client_id"]), ("AZURE_TENANT_ID", identity["tenant_id"])]
    egress = {AZURE_LOGIN}
    data = binding.get("databricks")
    if data:
        env += [("DATABRICKS_HOST", data["host"]), ("DATABRICKS_HTTP_PATH", data["http_path"])]
        env += [(name, data[key]) for name, key in (("DATABRICKS_CATALOG", "catalog"), ("DATABRICKS_SCHEMA", "schema")) if data.get(key)]
        egress.add(data["host"])
    return {"client_id": identity["client_id"], "env": env, "egress": sorted(egress), "secrets": secrets}
