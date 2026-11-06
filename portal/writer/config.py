import dataclasses as dc
import datetime as dt
import time

from ..github.appauth import app_jwt
from ..github.client import GitHub
from ..settings import DEFAULT_GATEWAYS

FACTORY_CHECKS, DEPLOY_CHECKS = ("factory-check",), ("deploy-check",)


@dc.dataclass(frozen=True)
class LifecycleApp:
    """The lifecycle App (spec 2026-11-02 L9): its id and private key (PORTAL_LIFECYCLE_APP_ID, PORTAL_LIFECYCLE_APP_KEY_FILE, in the
    writer's Secret only), and the API it calls. The App's JWT asks only for a repository's installation and a token for that one
    repository with administration: write; the token reads the repository, archives it (decided item 22), and is revoked. The key is never in a repr."""
    app_id: int
    key: bytes = dc.field(repr=False)
    api: str = "https://api.github.com"
    http: object = dc.field(default=None, repr=False, compare=False)

    def app_client(self) -> GitHub:
        return GitHub(self.api, self.http, lambda: app_jwt(self.app_id, self.key, int(time.time())))

    def repo_client(self, installation_id: int, name: str) -> GitHub:
        """A client whose token reaches only repository `name` of the installation, with only administration: write (and the metadata:
        read GitHub may add). A token GitHub grants wider or narrower than that is revoked unused (ValueError). The caller revokes the
        token it gets once it is done (revoke)."""
        doc = self.app_client().call("POST", f"/app/installations/{installation_id}/access_tokens", ok=(201,),
                                     json={"repositories": [name], "permissions": {"administration": "write"}}).json()
        token = doc.get("token") if isinstance(doc, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("no installation token")
        client = GitHub(self.api, self.http, lambda: token)
        permissions, repos = doc.get("permissions"), doc.get("repositories")
        if (permissions not in ({"administration": "write"}, {"administration": "write", "metadata": "read"})
                or doc.get("repository_selection") != "selected" or not isinstance(repos, list)
                or [r.get("name") if isinstance(r, dict) else None for r in repos] != [name]):
            revoke(client)
            raise ValueError("the token is not the one asked for")
        return client


def revoke(client: GitHub) -> None:
    """Best effort: DELETE /installation/token with the token itself. Nothing it raises reaches the caller."""
    try:
        client.call("DELETE", "/installation/token", ok=(204,))
    except Exception:  # noqa: BLE001 - best effort: the token expires within the hour anyway
        pass


@dc.dataclass(frozen=True)
class WriterConfig:
    org: str
    role_groups: dict
    app_id: int
    factory_checks: tuple = FACTORY_CHECKS  # PORTAL_WRITER_REQUIRED_CHECKS: the factory's, factory-check always among them
    deploy_checks: tuple = DEPLOY_CHECKS  # golden-road-deploy's: deploy-check (schema, signatures, compose drift, paths)
    merge_method: str = "squash"
    merge_timeout: dt.timedelta = dt.timedelta(minutes=30)  # how long a PR may wait for its checks, from when it opened
    confirm_grace: dt.timedelta = dt.timedelta(minutes=30)  # how long after the merge a definite 404 at the merge commit may last
    directory: object = None  # portal.directory.Directory (PORTAL_DIRECTORY): what a binding is re-checked against; None refuses bindings
    secret_server: object = None  # portal.secretserver_mock.MockSecretServer (PORTAL_SECRET_SERVER): what a binding's secrets are re-checked against
    repo_admins_stale_after: dt.timedelta = dt.timedelta(minutes=10)  # the portal's limit: a binding's requester must be a recent admin
    cloudflare: object = None  # portal.cloudflare_mock.MockCloudflare (PORTAL_CLOUDFLARE): what a binding's MCP servers are re-checked against
    mcp_gateways: dict = dc.field(default_factory=lambda: dict(DEFAULT_GATEWAYS))  # PORTAL_MCP_GATEWAY_TEST/_PROD, as the web reads them
    skills_catalog: object = None  # portal.skills_catalog.MockSkillsCatalog (PORTAL_SKILLS_CATALOG): what a binding's skills are written from
    lifecycle_app: LifecycleApp | None = None  # None: a decommission is refused at its recheck; suspend and resume don't need it
    write_login: str | None = None  # the write App's bot ("<app_slug>[bot]", from its installation at startup): the only author of a PR it adopts

    @property
    def targets(self) -> tuple[str, ...]:
        """Every repo the writer writes to: the factory (intake records) and golden-road-deploy (bindings and composed.yaml)."""
        return (f"{self.org}/scorecard-factory", f"{self.org}/golden-road-deploy")

    @property
    def required_checks(self) -> dict:
        """{target repo: the checks that must pass on the writer's PR there before it merges}."""
        return {f"{self.org}/scorecard-factory": tuple(self.factory_checks), f"{self.org}/golden-road-deploy": tuple(self.deploy_checks)}
