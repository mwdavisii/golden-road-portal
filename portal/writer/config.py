import dataclasses as dc
import datetime as dt

FACTORY_CHECKS, DEPLOY_CHECKS = ("factory-check",), ("deploy-check",)


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

    @property
    def targets(self) -> tuple[str, ...]:
        """Every repo the writer writes to: the factory (intake records) and golden-road-deploy (bindings and composed.yaml)."""
        return (f"{self.org}/scorecard-factory", f"{self.org}/golden-road-deploy")

    @property
    def required_checks(self) -> dict:
        """{target repo: the checks that must pass on the writer's PR there before it merges}."""
        return {f"{self.org}/scorecard-factory": tuple(self.factory_checks), f"{self.org}/golden-road-deploy": tuple(self.deploy_checks)}
