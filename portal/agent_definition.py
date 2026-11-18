"""Build an agent (spec 2026-11-17): the platform's models (portal/agent_models.json, held equal to platform.json's agent_models by
tests/pipeline/test_agent_models_parity.py), the Agent form's rules, the agent.yaml the writer writes, the template's link, and what an
environment's binding gives the runtime. Pure: the web, the writer and the collector share it."""

import functools
import json
from pathlib import Path

from .agent_file import AgentFileError, dump, load, problems

MODELS = Path(__file__).with_name("agent_models.json")
TEMPLATE_REPO = "golden-road-agent-template"
BRANCH = "portal/agent-definition"  # the writer's one branch for an agent repo (decided while planning: item 26)
PATH = "agent.yaml"
KIND = "agent_definition"
MAX_NAME, MAX_INSTRUCTIONS, MAX_STARTERS, MAX_STARTER = 64, 8000, 4, 200
HEADER = ("# Written by the golden road portal (request {id}): the agent's name, model, instructions and starters.\n"
          "# Change it on the app's Agent page in the portal.\n")
NAME_RULE = f"Display name: write 1 to {MAX_NAME} characters on one line."
MODEL_RULE = "Model: pick one of the platform's models."
INSTRUCTIONS_RULE = f"Instructions: write 1 to {MAX_INSTRUCTIONS} characters."
STARTERS_RULE = f"Conversation starters: at most {MAX_STARTERS}, each up to {MAX_STARTER} characters on one line."
SAVE_REFUSED = "The agent can't be saved: {why}"
# what the collector found wrong with an agent repo's agent.yaml (store.base.AGENT_PROBLEMS), in words for the pages
PROBLEM_WORDS = {"agent_yaml_missing": "there is none yet",
                 "agent_yaml_invalid": "it isn't a valid agent/v1 with one of the platform's models"}


class ModelsError(ValueError):
    pass


@functools.cache
def models() -> tuple:
    """({id, label}, ...) from portal/agent_models.json (agent-models/v1). Raises ModelsError for a file that isn't one."""
    doc = json.loads(MODELS.read_text(encoding="utf-8"))
    found = doc.get("models") if isinstance(doc, dict) and set(doc) == {"schema", "models"} and doc["schema"] == "agent-models/v1" else None
    if (not isinstance(found, list) or not found
            or not all(isinstance(m, dict) and set(m) == {"id", "label"} and isinstance(m["id"], str) and isinstance(m["label"], str)
                       for m in found)
            or len({m["id"] for m in found}) != len(found)):
        raise ModelsError("portal/agent_models.json is not a valid agent-models/v1")
    return tuple(dict(m) for m in found)


def model_ids() -> tuple:
    return tuple(m["id"] for m in models())


def _one_line(text: str, most: int) -> bool:
    return 1 <= len(text) <= most and "\n" not in text and "\r" not in text


def form_draft(display_name, model, instructions, starters) -> tuple:
    """(the agent/v1 draft, []) for a good form, or (None, [what to fix, each naming its field])."""
    name = (display_name or "").strip()
    text = (instructions or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    listed = starters is None or isinstance(starters, (list, tuple))  # a string is not a list of its characters
    lines = [s.strip() for s in starters or [] if isinstance(s, str) and s.strip()] if listed else []
    errors = []
    if not _one_line(name, MAX_NAME):
        errors.append(NAME_RULE)
    if model not in model_ids():
        errors.append(MODEL_RULE)
    if not 1 <= len(text) <= MAX_INSTRUCTIONS:
        errors.append(INSTRUCTIONS_RULE)
    if not listed or len(lines) > MAX_STARTERS or not all(_one_line(s, MAX_STARTER) for s in lines):
        errors.append(STARTERS_RULE)
    if errors:
        return None, errors
    draft = {"schema": "agent/v1", "display_name": name, "model": model, "instructions": text, "starters": lines}
    why = problems(draft, models=model_ids())  # the file's own rules on the document: control characters, the schema, the model
    if why is not None:
        return None, [SAVE_REFUSED.format(why=why)]
    try:
        load(dump(draft).encode("utf-8"), models=model_ids())  # and on the file as written: its size
    except AgentFileError as exc:
        return None, [SAVE_REFUSED.format(why=exc)]
    return draft, []


def agent_yaml(draft: dict, request_id: str) -> str:
    return dump(draft, HEADER.format(id=request_id))


def template_link(settings) -> str:
    return f"{settings.github_web}/{settings.org}/{TEMPLATE_REPO}/generate"


def preview(text: str, lines: int = 3, most: int = 300) -> str:
    """The first lines of the instructions, for the app page's card."""
    shown = "\n".join(text.split("\n")[:lines])[:most]
    return shown + ("…" if shown != text else "")


def runtime_view(binding) -> dict:
    """What the runtime gets from one environment's binding (decided while planning: item 33): its tools and its skills. Databricks
    needs the Azure identity too (the runtime's token is the identity's)."""
    b = binding or {}
    tools = [{"name": f"MCP server {s['name']}", "detail": f"its tools, as the gateway lists them ({s['server_id']})"}
             for s in sorted((b.get("mcp") or {}).get("servers") or [], key=lambda s: s["name"])]
    identity = b.get("azure_identity")
    if identity and b.get("databricks"):
        tools.append({"name": "databricks_query", "detail": f"one read-only SQL statement on {b['databricks']['directory_id']}"})
    if identity:
        tools.append({"name": "graph_read", "detail": f"Microsoft Graph reads as {identity['directory_id']}, under its granted scopes "
                                                    "(egress to graph.microsoft.com on 443)"})
    return {"tools": tools, "skills": sorted(s["name"] for s in b.get("skills") or [])}
