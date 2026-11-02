"""The skills picker in steps (spec 2026-10-31 Part 2, "The portal"): Environment, Skill, Review and save. Each step is a GET that carries
the choices so far, and every step asks the catalog again about the earlier ones, so a skipped or edited choice is refused, never
guessed. Only the final POST (routes_skills.add_skill) creates the binding request, through next_binding: in the catalog, approved for
the environment and the person's sign-in groups, not already bound, at most 20, and every bound skill together under 512 KiB. Step 3
runs next_binding as a dry run, so the review shows only what the POST would accept.

The environment is the first step's choice. The app page's link names its tab's environment, so it opens at step 2; "Choose another
environment" goes back to step 1 with that environment pre-chosen."""

import dataclasses as dc

from ..binding_record import ENVIRONMENTS, SKILL_NAME, BindingRefused, next_binding
from ..bindings import SKILLS_DIR

STEPS = ("Environment", "Skill", "Review and save")


class StepRefused(Exception):
    """A choice can't be used: show `step` again with `message`, answering `status`."""

    def __init__(self, step: int, message: str, status: int = 400):
        super().__init__(message)
        self.step, self.message, self.status = step, message, status


@dc.dataclass(frozen=True)
class Choices:
    environment: str | None = None
    name: str | None = None

    def up_to(self, step: int) -> "Choices":
        """Only what `step` needs to be shown: nothing for step 1, the environment for step 2."""
        return Choices() if step <= 1 else Choices(self.environment)


@dc.dataclass(frozen=True)
class Offered:
    """A skill at step 2: `bound` when the app already mounts it in this environment (Update, on the app page, re-pins it)."""
    name: str
    description: str
    bound: bool


def parse(params) -> Choices:
    """The choices from the query string. A later choice without the earlier one is refused."""
    environment, name = params.get("environment"), params.get("name")
    if environment is not None and environment not in ENVIRONMENTS:
        raise StepRefused(1, "The environment must be test or prod.")
    if name is not None and environment is None:
        raise StepRefused(1, "Choose an environment first.")
    if name is not None and not SKILL_NAME.fullmatch(name):
        raise StepRefused(2, "Choose a skill from the list.")
    return Choices(environment, name)


def view(catalog, groups, choices: Choices, *, binding=None) -> dict:
    """What the furthest step the choices reach shows: {"step", "environments", "main"} and, per step, "skills" (2), "added",
    "files" ([(mount path, bytes)]) and "size" (3). Raises StepRefused for a choice next_binding refuses. `binding` holds
    next_binding's other arguments (current, repo, org, directory, person_id, request_id, now, environment) for the chosen
    environment, for the dry run."""
    out = {"step": 1, "environments": list(ENVIRONMENTS), "main": catalog.main}
    if choices.environment is None:
        return out
    current = (binding or {}).get("current") or {}
    bound = {s["name"] for s in current.get("skills") or []}
    out.update(step=2, skills=[Offered(s.name, s.description, s.name in bound) for s in catalog.offered(choices.environment, groups)])
    if choices.name is None:
        return out
    try:  # a dry run of the final POST: every rule next_binding applies, and nothing is stored
        final = next_binding(add_skill=choices.name, skills_catalog=catalog, person_groups=tuple(groups), **binding)
    except BindingRefused as exc:
        raise StepRefused(2, str(exc)) from None
    added = next(s for s in final["skills"] if s["name"] == choices.name)
    files = catalog.skill(added["name"], added["commit"]).files
    out.update(step=3, added=added, files=[(f"{SKILLS_DIR}/{added['name']}/{path}", len(text.encode("utf-8"))) for path, text in sorted(files.items())],
               size=sum(len(text.encode("utf-8")) for text in files.values()))
    return out
