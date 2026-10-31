"""The secrets picker in steps (spec 2026-10-30 R7): Source, Secret, Field and name, Review and save. Each step is a GET that carries
the choices so far, and every step asks Secret Server again about the earlier ones, so a skipped or edited choice is refused, never
guessed. Only the final POST (routes_apps.add_secret) creates the binding request, through next_binding as before: the person's own
/summary, the platform's /summary, no file fields, reserved and duplicate names. Step 4 runs next_binding as a dry run, so the review
shows only what the POST would accept."""

import dataclasses as dc
import re

from ..binding_record import BindingRefused, SecretServerUnavailable, next_binding, secret_tokens
from ..secretserver import SecretServerError

STEPS = ("Source", "Secret", "Field and name", "Review and save")
SHOWN_MAX = 20  # search results shown: one page of the search, never every page
SEARCH_MAX = 100  # characters of search text
DIGITS = re.compile(r"[1-9][0-9]{0,9}")  # ASCII only: int() takes other scripts' digits
SECRET_SERVER_DOWN = "Secret Server could not answer: try again."


class StepRefused(Exception):
    """A choice can't be used: show `step` again with `message`, answering `status`."""

    def __init__(self, step: int, message: str, status: int = 400):
        super().__init__(message)
        self.step, self.message, self.status = step, message, status


@dc.dataclass(frozen=True)
class Choices:
    folder: int | None = None
    q: str = ""
    secret_id: int | None = None
    field_slug: str | None = None
    env: str | None = None

    def up_to(self, step: int) -> "Choices":
        """Only what `step` needs to be shown: nothing for step 1, the folder and search for 2, the secret as well for 3."""
        if step <= 1:
            return Choices()
        return Choices(self.folder, self.q) if step == 2 else Choices(self.folder, self.q, self.secret_id)


@dc.dataclass(frozen=True)
class Found:
    """A search result at step 2: `usable` when it is shared with the platform's account and has a field it can deliver."""
    id: int
    name: str
    template_name: str
    usable: bool
    reason: str


def parse(params) -> Choices:
    """The choices from the query string. A later choice without the earlier ones is refused."""
    folder, q, secret_id = params.get("folder"), params.get("q"), params.get("secret_id")
    field_slug, env = params.get("field_slug"), params.get("env")
    if folder is not None and not DIGITS.fullmatch(folder):
        raise StepRefused(1, "Choose a folder from the list.")
    if q is not None and len(q) > SEARCH_MAX:
        raise StepRefused(1, f"Search text is at most {SEARCH_MAX} characters.")
    if secret_id is not None and folder is None:
        raise StepRefused(1, "Start from the source: choose a folder first.")
    if secret_id is not None and not DIGITS.fullmatch(secret_id):
        raise StepRefused(1, "Choose a secret from the list.")
    if (field_slug is not None or env is not None) and secret_id is None:
        raise StepRefused(1, "Choose a secret first.")
    return Choices(int(folder) if folder is not None else None, q or "", int(secret_id) if secret_id is not None else None, field_slug, env)


def _results(client, platform_token, platform_user, found) -> list[Found]:
    fields, out = {}, []
    for s in found:
        if not client.shared_with_platform(platform_token, s.id):
            out.append(Found(s.id, s.name, s.template_name, False, f"Ask security to share it with {platform_user}."))
            continue
        if s.template_id not in fields:
            fields[s.template_id] = tuple(client.template_fields(platform_token, s.template_id))
        usable = bool(fields[s.template_id])
        out.append(Found(s.id, s.name, s.template_name, usable, "" if usable else "No field of this secret can be delivered (file fields never are)."))
    return out


def view(secret_server, secret_user, choices: Choices, *, binding: dict) -> dict:
    """What the furthest step the choices reach shows: {"step", "folders"} and, per step, "folder_name", "results" and "more" (2),
    "secret" and "fields" (3), "added" (4). Raises StepRefused for a choice Secret Server or next_binding refuses. `binding` holds
    next_binding's other arguments (current, repo, org, directory, person_id, request_id, now, environment) for the dry run."""
    try:
        with secret_tokens(secret_server, secret_user) as (client, person_token, platform_token):
            folders = client.folders(person_token)
            out = {"step": 1, "folders": folders}
            if choices.folder is None:
                return out
            if choices.folder not in folders:
                raise StepRefused(1, "Choose a folder from the list.")
            out.update(step=2, folder_name=folders[choices.folder])
            if choices.secret_id is None:
                found, more = client.search_first(person_token, text=choices.q, folder_id=choices.folder, take=SHOWN_MAX)
                out.update(results=_results(client, platform_token, secret_server.platform_user, found), more=more)
                return out
            summary = client.secret_summary(person_token, choices.secret_id)  # the person's own view of it
            if summary is None or summary.folder_id != choices.folder:
                raise StepRefused(2, "That secret is not one you can see in this folder: choose one from the list.")
            if not client.shared_with_platform(platform_token, summary.id):
                raise StepRefused(2, f"This secret isn't shared with {secret_server.platform_user}: ask security to share it.")
            fields = tuple(client.template_fields(platform_token, summary.template_id))
            if not fields:
                raise StepRefused(2, "No field of this secret can be delivered (file fields never are).")
            out.update(step=3, secret=summary, fields=fields)
    except SecretServerError:
        raise StepRefused(1, SECRET_SERVER_DOWN, 503) from None  # never the error's text
    if choices.field_slug is None and choices.env is None:
        return out
    try:  # a dry run of the final POST: every rule next_binding applies, and nothing is stored
        final = next_binding(add_secret={"secret_id": choices.secret_id, "field_slug": choices.field_slug or "", "env": choices.env or ""},
                             secret_server=secret_server, secret_user=secret_user, **binding)
    except SecretServerUnavailable:  # an outage during the dry run is not a refused choice
        raise StepRefused(1, SECRET_SERVER_DOWN, 503) from None
    except BindingRefused as exc:
        raise StepRefused(3, str(exc)) from None
    out.update(step=4, added=next(s for s in final["secrets"] if s["env"] == choices.env))
    return out
