"""Which env names a binding secret may use (spec 2026-10-29 Delinea secrets, D6). The portal can't import the builder, so it keeps a
copy of the builder's reserved names and prefixes (builder.spec.RESERVED_ENV and RESERVED_ENV_PREFIXES, which include the binding's
AZURE_, DATABRICKS_ and SECRETSERVER_); tests/pipeline/test_portal_env_names_parity.py holds the copy to builder.spec. A name the
app's own builder-spec.json sets is compose's to refuse: the writer composes with the release's manifests."""

import re

ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")  # binding/v2's $defs.env_name
RESERVED = frozenset({"PORT", "NODE_ENV", "NODE_OPTIONS", "PATH", "HOME"})
RESERVED_PREFIXES = ("LD_", "PYTHON", "GUNICORN_", "UVICORN_", "NODE_", "NPM_", "AZURE_", "DATABRICKS_", "SECRETSERVER_")


def refused(name) -> str | None:
    """Why a binding secret may not use this env name, or None when it may."""
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
        return "Name the variable in capitals, digits and underscores, starting with a letter or underscore (at most 128)."
    if name in RESERVED or name.startswith(RESERVED_PREFIXES):
        return f"{name} is set by the platform and can't hold a secret."
    return None
