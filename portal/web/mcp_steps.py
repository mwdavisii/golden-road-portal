"""The MCP server picker in steps (spec 2026-10-31 Part 1, "The portal"): Environment, Server, Review and save. Each step is a GET that
carries the choices so far, and every step asks Cloudflare again about the earlier ones, so a skipped or edited choice is refused, never
guessed. Only the final POST (routes_mcp.add_server) creates the binding request, through next_binding: on the environment's gateway,
ready, not one that acts as each signed-in person, one the person may use, not already bound, at most 20. Step 3 runs next_binding
as a dry run, so the review shows only what the POST would accept.

The environment is the first step's choice. The app page's link names its tab's environment, so it opens at step 2; "Choose another
environment" goes back to step 1 with that environment pre-chosen."""

import dataclasses as dc

from ..binding_record import CLOUDFLARE_DOWN, ENVIRONMENTS, ON_BEHALF, SERVER_ID, BindingRefused, CloudflareUnavailable, next_binding
from ..bindings import effects
from ..cloudflare import CloudflareError

STEPS = ("Environment", "Server", "Review and save")
NO_EMAIL = "Your sign-in has no email address, so Cloudflare can't tell which servers you may use."
BOUND = "Already on this app"


class StepRefused(Exception):
    """A choice can't be used: show `step` again with `message`, answering `status`."""

    def __init__(self, step: int, message: str, status: int = 400):
        super().__init__(message)
        self.step, self.message, self.status = step, message, status


@dc.dataclass(frozen=True)
class Choices:
    environment: str | None = None
    server_id: str | None = None

    def up_to(self, step: int) -> "Choices":
        """Only what `step` needs to be shown: nothing for step 1, the environment for step 2."""
        return Choices() if step <= 1 else Choices(self.environment)


@dc.dataclass(frozen=True)
class Offered:
    """A server at step 2: `usable` when an app may have it; otherwise `reason` says why not."""
    server_id: str
    name: str
    description: str
    tools: tuple
    usable: bool
    reason: str
    bound: bool = False


def parse(params) -> Choices:
    """The choices from the query string. A later choice without the earlier one is refused."""
    environment, server_id = params.get("environment"), params.get("server_id")
    if environment is not None and environment not in ENVIRONMENTS:
        raise StepRefused(1, "The environment must be test or prod.")
    if server_id is not None and environment is None:
        raise StepRefused(1, "Choose an environment first.")
    if server_id is not None and not SERVER_ID.fullmatch(server_id):
        raise StepRefused(2, "Choose a server from the list.")
    return Choices(environment, server_id)


def _offered(gateway, mine, bound) -> list[Offered]:
    """The gateway's servers the person may use whose status is ready (spec 2026-10-31, the plan's M2 filtering): a server that acts
    as each signed-in person is greyed with the reason, one already bound is marked; nothing else is listed."""
    out = []
    for s in gateway.servers:
        if s.server_id not in mine or s.status != "ready":
            continue
        reason = ON_BEHALF if s.on_behalf else BOUND if s.server_id in bound else ""
        out.append(Offered(s.server_id, s.name, s.description, s.tools, not reason, reason, s.server_id in bound))
    return out


def view(cloudflare, gateways, email, choices: Choices, *, binding=None) -> dict:
    """What the furthest step the choices reach shows: {"step", "environments"} and, per step, "gateway", "servers" and "no_email"
    (2), "added", "url", "servers_after" and "token" (3). Raises StepRefused for a choice Cloudflare or next_binding refuses.
    `binding` holds next_binding's other arguments (current, repo, org, directory, person_id, request_id, now, environment) for the
    chosen environment, for the dry run."""
    out = {"step": 1, "environments": [(env, gateways[env]) for env in ENVIRONMENTS]}
    if choices.environment is None:
        return out
    host = gateways[choices.environment]
    try:
        with cloudflare.client() as client:
            gateway = client.gateway(host)
    except CloudflareError:
        raise StepRefused(1, CLOUDFLARE_DOWN, 503) from None  # never the error's text
    if gateway is None:
        raise StepRefused(1, f"The {choices.environment} MCP gateway ({host}) is not in Cloudflare.")
    current = (binding or {}).get("current") or {}
    bound = {s["server_id"] for s in (current.get("mcp") or {}).get("servers") or []}
    mine = cloudflare.servers_for(email)
    out.update(step=2, gateway=host, servers=_offered(gateway, mine, bound), no_email=not email)
    if choices.server_id is None:
        return out
    try:  # a dry run of the final POST: every rule next_binding applies, and nothing is stored
        final = next_binding(add_mcp=choices.server_id, cloudflare=cloudflare, mcp_gateway=host, person_email=email, **binding)
    except CloudflareUnavailable:  # an outage during the dry run is not a refused choice
        raise StepRefused(1, CLOUDFLARE_DOWN, 503) from None
    except BindingRefused as exc:
        raise StepRefused(2, str(exc)) from None
    mcp = effects(final)["mcp"]
    out.update(step=3, added=next(s for s in final["mcp"]["servers"] if s["server_id"] == choices.server_id), url=mcp["url"],
               servers_after=",".join(mcp["servers"]), token=mcp["token"])
    return out
