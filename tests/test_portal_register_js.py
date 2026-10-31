import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "portal/static/register.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def frag(doc):
    raw = json.dumps(doc).encode() if not isinstance(doc, bytes) else doc
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def parse(fragment):
    script = f"const m = require({json.dumps(str(JS))}); process.stdout.write(JSON.stringify(m.parseDraft({json.dumps(fragment)})));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


GOOD = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores notes. <b>bold</b>", "data_classification": "public"}


@node
def test_a_good_draft_is_read_as_text():
    result = parse(frag(GOOD))
    assert result["ok"] and result["draft"]["purpose"] == "Stores notes. <b>bold</b>"
    assert result["notes"] == []


@node
@pytest.mark.parametrize("fragment, error", [
    ("", "empty"), ("a" * 16385, "too_large"), ("not base64!", "not_base64url"), (frag(b"not json"), "not_json"),
    (frag([1, 2]), "not_object"), (frag({**GOOD, "owner": "p_x"}), "unknown_field"), (frag({**GOOD, "schema": "x"}), "schema"),
    (frag({**GOOD, "repo": "acme"}), "repo"), (frag({**GOOD, "purpose": "x" * 4001}), "purpose"),
    (frag({**GOOD, "data_classification": "secret"}), "data_classification"),
    (frag(b'{"schema": "registration-draft/v1", "repo": "acme/notes", "__proto__": {"x": 1}}'), "unknown_field"),
])
def test_a_bad_draft_is_refused_with_a_reason(fragment, error):
    assert parse(fragment) == {"ok": False, "error": error}


def test_the_script_never_writes_html_or_runs_code():
    text = JS.read_text()
    sinks = ("inner" + "HTML", "outer" + "HTML", "insertAdjacent" + "HTML", "document." + "write", "ev" + "al(", "Func" + "tion(", "fet" + "ch(",
             "XMLHttp" + "Request")
    for sink in sinks:
        assert sink not in text, sink
    assert "sessionStorage" in text and "history.replaceState" in text


def utf8_frag(doc):
    """As `golden-road register` writes it: UTF-8, not \\u escapes."""
    return frag(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@node
def test_the_purpose_cap_counts_characters_the_way_python_does_not_utf16_units():
    """Python counts code points (4000 is the cap on the server). 2500 emoji are 5000 UTF-16 units in JavaScript, so counting units
    would refuse a purpose the server accepts. (4000 emoji can't arrive at all: the fragment cap of 16384 characters stops them first.)"""
    assert parse(utf8_frag({**GOOD, "purpose": "\U0001F600" * 2500}))["ok"] is True
    assert parse(utf8_frag({**GOOD, "purpose": "\u4e2d" * 4000}))["ok"] is True
    assert parse(utf8_frag({**GOOD, "purpose": "\u4e2d" * 4001})) == {"ok": False, "error": "purpose"}
    assert parse(utf8_frag({**GOOD, "purpose": "\U0001F600" * 4000})) == {"ok": False, "error": "too_large"}


@node
@pytest.mark.parametrize("old, new", [("internal", "confidential"), ("restricted", "confidential"), ("phi", "protected")])
def test_a_draft_with_a_retired_classification_is_used_without_it_and_says_why(old, new):
    result = parse(frag({**GOOD, "data_classification": old}))
    assert result["ok"] and result["draft"]["data_classification"] == ""
    assert result["notes"] == [f"`{old}` is retired: use `{new}`. Choose the data classification yourself."]


@node
@pytest.mark.parametrize("value", [["phi"], {"internal": 1}, 7])
def test_a_classification_that_is_not_a_string_is_refused_even_if_it_reads_as_a_retired_name(value):
    assert parse(frag({**GOOD, "data_classification": value})) == {"ok": False, "error": "data_classification"}


@node
@pytest.mark.parametrize("tier", ["low", "moderate", 3, None, "<b>x</b>"])
def test_an_old_link_with_a_proposed_tier_is_accepted_and_the_tier_ignored(tier):
    result = parse(frag({**GOOD, "proposed_tier": tier}))
    assert result["ok"] and "proposed_tier" not in result["draft"]


def test_the_script_fills_the_classification_radio_group():
    text = JS.read_text()
    assert "form.elements.data_classification" in text and ".value = d.data_classification" in text


FILL = """
var events = [];
function el(extra) { return Object.assign({ value: "", dispatchEvent: function (e) { events.push(e.type); } }, extra || {}); }
global.Event = function (type, opts) { this.type = type; this.bubbles = !!(opts && opts.bubbles); };
var repo = { options: [{ value: "acme/notes" }], selectedIndex: -1 };
var form = { elements: { repo: repo, purpose: el(), data_classification: el() } };
var status = { hidden: true, textContent: "" };
"""


@node
def test_filling_from_a_draft_tells_the_purpose_s_counter():
    script = (f"const m = require({json.dumps(str(JS))});" + FILL
              + f"m.fill(form, status, m.parseDraft({json.dumps(frag(GOOD))}));"
              + "process.stdout.write(JSON.stringify({events: events, purpose: form.elements.purpose.value, shown: !status.hidden}));")
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == {"events": ["input"], "purpose": "Stores notes. <b>bold</b>", "shown": True}


def test_the_script_s_levels_are_the_contracts():
    """register.js can't import classification.py: its lists are held equal here (review minor 5)."""
    import re as _re
    from portal.classification import LEVELS, RETIRED
    text = JS.read_text()
    levels = _re.search(r"var CLASSIFICATIONS = (\[[^\]]*\]);", text).group(1)
    retired = _re.search(r"var RETIRED = (\{[^}]*\});", text).group(1)
    assert json.loads(levels) == list(LEVELS)
    assert json.loads(_re.sub(r"(\w+):", r'"\1":', retired)) == RETIRED


@node
@pytest.mark.parametrize("level", ["confidential", "protected"])
def test_a_draft_with_a_current_level_is_used(level):
    result = parse(frag({**GOOD, "data_classification": level}))
    assert result["ok"] and result["draft"]["data_classification"] == level and result["notes"] == []


# ---- the change page's card belongs to one repo ----

CARD = """
var events = [];
global.Event = function (type, opts) { this.type = type; };
function el(value) { return { value: value, defaultValue: value, dispatchEvent: function (e) { events.push(e.type); } }; }
function radio(value, checked) { return { value: value, checked: checked, defaultChecked: checked }; }
var radios = [radio("public", false), radio("confidential", true)];
var repo = { options: [{ value: "acme/notes" }, { value: "acme/tools" }], selectedIndex: 1 };
var form = { elements: { repo: repo, purpose: el("Builds internal tools."), data_classification: radios } };
var attrs = { "data-repo": "acme/tools", "data-prefilled": "true" };
var card = { hidden: false, getAttribute: function (n) { return attrs[n]; }, setAttribute: function (n, v) { attrs[n] = v; } };
var status = { hidden: true, textContent: "" };
"""


def run_card(steps):
    script = (f"const m = require({json.dumps(str(JS))});" + CARD + steps
              + "process.stdout.write(JSON.stringify({card: card.hidden, purpose: form.elements.purpose.value, "
                "checked: radios.filter(function (r) { return r.checked; }).map(function (r) { return r.value; }), status: status.textContent}));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@node
def test_choosing_another_repo_hides_the_card_and_clears_its_pre_fill():
    out = run_card('repo.selectedIndex = 0; m.syncCard(form, card, status);')
    assert out["card"] is True and out["purpose"] == "" and out["checked"] == [] and "acme/tools" in out["status"]
    again = run_card('repo.selectedIndex = 0; m.syncCard(form, card, status); repo.selectedIndex = 1; m.syncCard(form, card, status);')
    assert again["card"] is False and again["purpose"] == ""   # shown again, never filled again


@node
def test_what_the_person_wrote_is_kept_when_the_repo_changes():
    out = run_card('form.elements.purpose.value = "My own words."; radios[0].checked = true; radios[1].checked = false;'
                   'repo.selectedIndex = 0; m.syncCard(form, card, status);')
    assert out["card"] is True and out["purpose"] == "My own words." and out["checked"] == ["public"]


@node
def test_a_draft_for_another_repo_hides_the_card_and_one_for_this_repo_says_it_replaced_the_text():
    other = frag({**GOOD, "repo": "acme/notes"})
    out = run_card(f'repo.selectedIndex = 0; m.fill(form, status, m.parseDraft({json.dumps(other)}), card);')
    assert out["card"] is True and out["purpose"] == GOOD["purpose"]
    same = frag({**GOOD, "repo": "acme/tools"})
    out = run_card(f'm.fill(form, status, m.parseDraft({json.dumps(same)}), card);')
    assert out["card"] is False and "replaced the text of the current registration" in out["status"]
