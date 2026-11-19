"""agent.yaml (agent/v1, spec 2026-11-17): the one file an agent repo holds. The builder (src/contracts/agent_file.py), the portal
(portal/agent_file.py) and the agent runtime (agent_runtime/agent_file.py) hold this file byte for byte
(tests/pipeline/test_agent_file_parity.py), so the three read the same file the same way. SCHEMA is agent/v1; the contracts' JSON
file (src/contracts/schemas/agent.v1.json) is held equal to it by tests/contracts/test_agent_file.py. An error names the field and
why, never the file's text."""

import yaml
from jsonschema import Draft202012Validator

MAX_BYTES = 16 * 1024
KEYS = ("schema", "display_name", "model", "instructions", "starters")
# C0 and C1 controls (C1's NEL, \x85, reads back from YAML as a line break), the line and paragraph separators in a one-line field, and
# lone surrogates (they are not Unicode text: encoding them as UTF-8 fails later, in a page or a database).
SURROGATE = "\\ud800-\\udfff"
LINE_CONTROL = f"[\\x00-\\x1f\\x7f-\\x9f\\u2028\\u2029{SURROGATE}]"  # a one-line field holds none of these
TEXT_CONTROL = f"[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f{SURROGATE}]"  # the instructions keep tab and line feed only
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://platform.internal/schemas/agent.v1.json",
    "title": "agent/v1",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema", "display_name", "model", "instructions"],
    "properties": {
        "schema": {"const": "agent/v1"},
        "display_name": {"type": "string", "minLength": 1, "maxLength": 64, "pattern": "\\S", "not": {"pattern": LINE_CONTROL}},
        "model": {"type": "string", "maxLength": 64, "pattern": "^[a-z0-9][a-z0-9._-]{0,63}$", "not": {"pattern": LINE_CONTROL}},
        "instructions": {"type": "string", "minLength": 1, "maxLength": 8000, "pattern": "\\S", "not": {"pattern": TEXT_CONTROL}},
        "starters": {"type": "array", "maxItems": 4,
                     "items": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": "\\S", "not": {"pattern": LINE_CONTROL}}},
    },
}
_VALIDATOR = Draft202012Validator(SCHEMA)


class AgentFileError(ValueError):
    """Why agent.yaml can't be used: the field and why, in words written here."""


class _Loader(yaml.SafeLoader):
    """SafeLoader that refuses a key that isn't text (a list or mapping key can't be a dict key at all) and a mapping naming one key
    twice (PyYAML keeps the last; a reader may see the first)."""

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True) if isinstance(key_node, yaml.ScalarNode) else None
                if not isinstance(key, str):
                    raise AgentFileError("agent.yaml has a key that isn't text")
                if key in seen:
                    raise AgentFileError("agent.yaml names a key twice")
                seen.add(key)
        return super().construct_mapping(node, deep)


def _why(error) -> str:
    kind, value = error.validator, error.validator_value
    if kind == "required":
        return "is missing a required key (schema, display_name, model and instructions are required)"
    if kind == "additionalProperties":
        return "has a key agent/v1 doesn't know (it knows schema, display_name, model, instructions and starters)"
    if kind == "const":
        return "must be agent/v1"
    if kind == "type":
        return f"must be {'a list' if value == 'array' else 'text' if value == 'string' else 'a mapping'}"
    if kind == "minLength":
        return "is empty"
    if kind == "maxLength":
        return f"is too long (at most {value} characters)"
    if kind == "maxItems":
        return f"has more than {value} items"
    if kind == "pattern" and value == "\\S":
        return "is blank"
    if kind == "not":
        if isinstance(error.instance, str) and any("\ud800" <= c <= "\udfff" for c in error.instance):
            return "has an unpaired surrogate (text that isn't valid Unicode)"
        return "has a control character"
    return "has a character it can't hold"


def load(raw: bytes, models=None) -> dict:
    """The agent/v1 document in `raw` (agent.yaml's bytes). With `models` (the platform's model ids), the model must be one of them.
    Raises AgentFileError."""
    if not isinstance(raw, bytes):
        raise AgentFileError("agent.yaml must be read as bytes")
    if len(raw) > MAX_BYTES:
        raise AgentFileError(f"agent.yaml is larger than {MAX_BYTES // 1024} KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AgentFileError("agent.yaml is not UTF-8 text") from None
    try:
        documents = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                raise AgentFileError("agent.yaml uses YAML anchors or aliases")
            documents += isinstance(event, yaml.DocumentStartEvent)
        if documents > 1:  # an empty or null document beside the mapping is refused too: one file, one document
            raise AgentFileError("agent.yaml must hold one YAML document")
        docs = list(yaml.load_all(text, Loader=_Loader))
    except AgentFileError:
        raise
    except (yaml.YAMLError, RecursionError, TypeError, ValueError):  # a bad tagged value (!!int x) raises ValueError or TypeError
        raise AgentFileError("agent.yaml is not valid YAML") from None
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise AgentFileError("agent.yaml must hold exactly one mapping")
    doc = docs[0]
    why = problems(doc, models)
    if why is not None:
        raise AgentFileError(why)
    return doc


def problems(doc, models=None) -> str | None:
    """Why `doc` (a document, as load returns one) isn't a valid agent/v1, in load's words (the field and why), or None. With `models`,
    the model must be one of them. The file's own limits (its size, YAML) are load's: this checks the document as it is."""
    if not isinstance(doc, dict):
        return "agent.yaml must hold exactly one mapping"
    try:
        errors = sorted(_VALIDATOR.iter_errors(doc), key=lambda e: (len(e.absolute_path), [str(p) for p in e.absolute_path]))
    except (TypeError, ValueError, RecursionError):
        return "agent.yaml can't be checked against agent/v1"
    if errors:
        where = ".".join(str(p) for p in errors[0].absolute_path) or "the file"
        return f"agent.yaml: {where} {_why(errors[0])}"
    if models is not None and doc["model"] not in models:
        return f"agent.yaml: model is not one of the platform's models ({', '.join(models)})"
    return None


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _text(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _text)


def dump(doc: dict, header: str = "") -> str:
    """agent.yaml's text for a valid agent/v1 document: `header` (comment lines), then the keys in agent/v1's order, multi-line
    text as literal blocks (PyYAML falls back to a quoted style when a block can't hold the text exactly). Refuses a document load
    would refuse: its text could be written and read back as another (a NEL in a literal block reads back as a line break)."""
    if not _VALIDATOR.is_valid(doc):
        raise AgentFileError("agent.yaml can't be written: the document isn't valid agent/v1")
    ordered = {key: doc[key] for key in KEYS if key in doc}
    return header + yaml.dump(ordered, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=False)
