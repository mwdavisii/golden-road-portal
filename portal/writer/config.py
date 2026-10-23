import dataclasses as dc
import datetime as dt


@dc.dataclass(frozen=True)
class WriterConfig:
    org: str
    role_groups: dict
    app_id: int
    required_checks: tuple = ("factory-check",)
    merge_method: str = "squash"
    merge_timeout: dt.timedelta = dt.timedelta(minutes=30)  # how long a PR may wait for its checks, from when it opened
    confirm_grace: dt.timedelta = dt.timedelta(minutes=30)  # how long after the merge a definite 404 at the merge commit may last

    @property
    def targets(self) -> tuple[str, ...]:
        return (f"{self.org}/scorecard-factory",)
