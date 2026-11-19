"""The Agent form's rules and what the portal writes (spec 2026-11-17 "What the person sees", Errors)."""

import pytest
from portal_fakes import make_settings

from portal import agent_definition as ad
from portal.agent_file import load

GOOD = {"display_name": "Calendar helper", "model": "chat-default", "instructions": "You help the team.\r\nBe brief.",
        "starters": ["When is everyone free?", "", "  "]}


def test_a_good_form_is_an_agent_v1_draft():
    draft, errors = ad.form_draft(**GOOD)
    assert errors == [] and draft == {"schema": "agent/v1", "display_name": "Calendar helper", "model": "chat-default",
                                      "instructions": "You help the team.\nBe brief.", "starters": ["When is everyone free?"]}


@pytest.mark.parametrize("change, rule", [
    ({"display_name": ""}, ad.NAME_RULE), ({"display_name": "x" * 65}, ad.NAME_RULE), ({"display_name": "a\nb"}, ad.NAME_RULE),
    ({"model": "other"}, ad.MODEL_RULE), ({"model": ""}, ad.MODEL_RULE),
    ({"instructions": "   "}, ad.INSTRUCTIONS_RULE), ({"instructions": "x" * 8001}, ad.INSTRUCTIONS_RULE),
    ({"starters": ["a", "b", "c", "d", "e"]}, ad.STARTERS_RULE), ({"starters": ["x" * 201]}, ad.STARTERS_RULE),
])
def test_each_refusal_names_its_field(change, rule):
    draft, errors = ad.form_draft(**{**GOOD, **change})
    assert draft is None and errors == [rule]


def test_a_control_character_is_refused_by_the_file_s_own_rules():
    draft, errors = ad.form_draft(**{**GOOD, "instructions": "Help.\x07"})
    assert draft is None and errors == ["The agent can't be saved: agent.yaml: instructions has a control character"]


@pytest.mark.parametrize("change, field", [({"display_name": "Hel\x85per"}, "display_name"), ({"display_name": "Helper\u2028x"}, "display_name"),
                                           ({"starters": ["Hi\x1b"]}, "starters.0"), ({"instructions": "Help.\x85More."}, "instructions")])
def test_the_file_s_refusal_names_the_field_too(change, field):
    """dump refuses an invalid document, so the form names the field from the file's own reading of it."""
    draft, errors = ad.form_draft(**{**GOOD, **change})
    assert draft is None and len(errors) == 1 and errors[0].startswith(f"The agent can't be saved: agent.yaml: {field} has a")


def test_a_file_too_large_is_refused():
    draft, errors = ad.form_draft(**{**GOOD, "instructions": "\u4e00" * 7000})  # 7000 characters, 21000 bytes of UTF-8
    assert draft is None and errors == ["The agent can't be saved: agent.yaml is larger than 16 KiB"]


def test_the_written_file_reads_back_and_names_its_request():
    draft, _ = ad.form_draft(**GOOD)
    text = ad.agent_yaml(draft, "r_" + "a" * 32)
    assert text.startswith("# Written by the golden road portal (request r_" + "a" * 32 + ")")
    assert load(text.encode(), models=ad.model_ids()) == draft


def test_the_models_come_from_the_platform_s_file():
    assert ad.models() == ({"id": "chat-default", "label": "Default chat model"},) and ad.model_ids() == ("chat-default",)


def test_the_template_link_is_github_s_generate_page():
    assert ad.template_link(make_settings()) == "https://github.com/acme/golden-road-agent-template/generate"


def test_what_the_runtime_gets_follows_the_binding():
    assert ad.runtime_view(None) == {"tools": [], "skills": []}
    binding = {"azure_identity": {"directory_id": "sp-notes"}, "databricks": {"directory_id": "wh-notes"},
               "mcp": {"gateway": "mcp-test.example.com", "servers": [{"server_id": "notes", "name": "Notes"}, {"server_id": "docs", "name": "Docs"}]},
               "skills": [{"name": "triage", "commit": "a" * 40}, {"name": "alpha", "commit": "b" * 40}]}
    view = ad.runtime_view(binding)
    assert [t["name"] for t in view["tools"]] == ["MCP server Docs", "MCP server Notes", "databricks_query", "graph_read"]
    assert view["skills"] == ["alpha", "triage"]
    assert [t["name"] for t in ad.runtime_view({"databricks": {"directory_id": "wh"}})["tools"]] == []  # no identity, no Databricks


def test_the_preview_is_the_first_lines():
    assert ad.preview("one\ntwo\nthree\nfour") == "one\ntwo\nthree…" and ad.preview("x" * 400) == "x" * 300 + "…"
    assert ad.preview("short") == "short"


@pytest.mark.parametrize("instructions", ["Help \U0001F600.\x07", "\u00e9" * 3000 + "\x00"], ids=["emoji", "nul-and-accents"])
def test_the_reason_is_the_document_s_own_not_an_encoding_s(instructions):
    """An emoji is not an unpaired surrogate, and NUL beside non-ASCII text is a control character, not a file too large."""
    draft, errors = ad.form_draft(**{**GOOD, "instructions": instructions})
    assert draft is None and errors == ["The agent can't be saved: agent.yaml: instructions has a control character"]


@pytest.mark.parametrize("starters", ["abc", {"a": 1}, 7])
def test_starters_are_a_list(starters):
    draft, errors = ad.form_draft(**{**GOOD, "starters": starters})
    assert draft is None and errors == [ad.STARTERS_RULE]


def test_starters_may_be_a_tuple_or_none():
    assert ad.form_draft(**{**GOOD, "starters": ("Hi",)})[0]["starters"] == ["Hi"]
    assert ad.form_draft(**{**GOOD, "starters": None})[0]["starters"] == []
