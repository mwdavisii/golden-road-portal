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


GOOD = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores notes. <b>bold</b>", "data_classification": "internal",
        "proposed_tier": "low"}


@node
def test_a_good_draft_is_read_as_text():
    result = parse(frag(GOOD))
    assert result["ok"] and result["draft"]["purpose"] == "Stores notes. <b>bold</b>"


@node
@pytest.mark.parametrize("fragment, error", [
    ("", "empty"), ("a" * 16385, "too_large"), ("not base64!", "not_base64url"), (frag(b"not json"), "not_json"),
    (frag([1, 2]), "not_object"), (frag({**GOOD, "owner": "p_x"}), "unknown_field"), (frag({**GOOD, "schema": "x"}), "schema"),
    (frag({**GOOD, "repo": "acme"}), "repo"), (frag({**GOOD, "purpose": "x" * 4001}), "purpose"),
    (frag({**GOOD, "data_classification": "secret"}), "data_classification"), (frag({**GOOD, "proposed_tier": 3}), "proposed_tier"),
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
