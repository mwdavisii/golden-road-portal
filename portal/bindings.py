"""What an app's binding (binding/v1) adds to the container: its Azure workload identity's client id, plain environment variables,
and the hosts it must reach on 443. The release render applies it to the signed manifests; the portal shows the same thing on the
app's page. The two packages hold this file byte for byte (a repo test keeps them equal), so they can't disagree. The input is a
document that already validated as binding/v1."""

AZURE_LOGIN = "login.microsoftonline.com"  # where the workload identity's token is exchanged


def effects(binding) -> dict:
    """{"client_id", "env": [(name, value)], "egress": [host]} for a binding; nothing without an Azure identity, because the
    Databricks connection authenticates as that identity. A fresh dict every call, so no caller can change another's."""
    identity = (binding or {}).get("azure_identity")
    if not identity:
        return {"client_id": None, "env": [], "egress": []}
    env = [("AZURE_CLIENT_ID", identity["client_id"]), ("AZURE_TENANT_ID", identity["tenant_id"])]
    egress = {AZURE_LOGIN}
    data = binding.get("databricks")
    if data:
        env += [("DATABRICKS_HOST", data["host"]), ("DATABRICKS_HTTP_PATH", data["http_path"])]
        env += [(name, data[key]) for name, key in (("DATABRICKS_CATALOG", "catalog"), ("DATABRICKS_SCHEMA", "schema")) if data.get(key)]
        egress.add(data["host"])
    return {"client_id": identity["client_id"], "env": env, "egress": sorted(egress)}
