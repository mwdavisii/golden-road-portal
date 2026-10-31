"""forms.js: the counts and the early reasons. The server decides; these only say it sooner (spec 2026-10-30 R3, R8)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "portal/static/forms.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
TIERS, CLASSES = ["low", "moderate", "high"], ["public", "confidential", "protected"]


def call(name, *args):
    script = f"const m = require({json.dumps(str(JS))}); process.stdout.write(JSON.stringify(m.{name}(...{json.dumps(list(args))})));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@node
def test_count_label_counts_code_points_and_shows_a_minimum():
    assert call("countLabel", "abc", 0, 4000) == "3 / 4000"
    assert call("countLabel", "中文", 30, 1000) == "2 / 30 minimum"
    assert call("countLabel", "x" * 30, 30, 1000) == "30 / 1000"


@node
def test_below_matches_the_server_s_floor():
    assert call("below", TIERS, CLASSES, "low", "public", "moderate", "confidential") == ["tier", "data classification"]
    assert call("below", TIERS, CLASSES, "high", "protected", "moderate", "confidential") == []
    assert call("below", TIERS, CLASSES, "", "", "moderate", "confidential") == []


@node
@pytest.mark.parametrize("fields, problem", [
    ({"tier": "", "classification": "public", "justification": "x" * 20, "override": "", "below": []}, "Choose a tier."),
    ({"tier": "low", "classification": "", "justification": "x" * 20, "override": "", "below": []}, "Choose a data classification."),
    ({"tier": "low", "classification": "public", "justification": "short", "override": "", "below": []},
     "Give a justification of 10 to 1000 characters."),
    ({"tier": "low", "classification": "public", "justification": "x" * 20, "override": "too short", "below": ["tier"]},
     "The tier is below the floor: give a reason of 30 to 1000 characters."),
    ({"tier": "low", "classification": "public", "justification": "x" * 20, "override": "y" * 30, "below": ["tier"]}, ""),
    ({"tier": "high", "classification": "protected", "justification": "x" * 20, "override": "", "below": []}, ""),
])
def test_approve_problem_says_why_approve_would_be_refused(fields, problem):
    assert call("approveProblem", fields) == problem


def test_the_script_never_writes_html_or_runs_code():
    text = JS.read_text()
    for sink in ("inner" + "HTML", "outer" + "HTML", "insertAdjacent" + "HTML", "document." + "write", "ev" + "al(", "Func" + "tion(", "fet" + "ch("):
        assert sink not in text, sink


# A stand-in for a form: just what wireApproval reads. `missing` drops one element or attribute, as a hand-edited page might.
FAKE_FORM = """
function fakeForm(missing) {
  var attrs = { "data-tiers": "low moderate high", "data-classifications": "public confidential protected",
                "data-floor-tier": "low", "data-floor-classification": "public" };
  var button = { disabled: false, setAttribute: function () {} }, why = { textContent: "" }, override = { hidden: false };
  var found = { "[data-approve]": button, "[data-approve-why]": why, "[data-override]": override };
  var form = {
    wired: false, button: button, why: why,
    elements: { justification: { value: "" }, floor_override_reason: { value: "" } },
    getAttribute: function (n) { return n === missing ? null : (attrs[n] === undefined ? null : attrs[n]); },
    querySelector: function (s) { return s === missing ? null : (found[s] || null); },
    addEventListener: function () { form.wired = true; }
  };
  if (missing === "justification") delete form.elements.justification;
  return form;
}
"""


def wire(missing):
    script = (f"const m = require({json.dumps(str(JS))});" + FAKE_FORM
              + f"var bad = fakeForm({json.dumps(missing)}), good = fakeForm(null); var errors = m.wireForms([bad, good]);"
              + "process.stdout.write(JSON.stringify({bad: bad.wired, good: good.wired, disabled: good.button.disabled, why: good.why.textContent, errors: errors}));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@node
@pytest.mark.parametrize("missing", ["data-tiers", "data-floor-tier", "[data-approve]", "[data-approve-why]", "justification"])
def test_a_form_missing_a_part_is_left_alone_and_the_others_are_still_wired(missing):
    assert wire(missing) == {"bad": False, "good": True, "disabled": True, "why": "Choose a tier.", "errors": 0}


@node
def test_the_early_check_trims_only_spaces_and_tabs():
    """The server decides; a justification padded with newlines is reported as more than one line, not trimmed into one."""
    base = {"tier": "low", "classification": "public", "override": "", "below": []}
    assert call("approveProblem", {**base, "justification": " \t" + "x" * 10 + "\t "}) == ""
    assert call("approveProblem", {**base, "justification": "x" * 10 + "\n"}) == "Give a justification of 10 to 1000 characters."
