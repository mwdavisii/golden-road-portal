"""Start here (spec 2026-10-30 R2): the three prerequisites as a checklist with a progress bar. The ticks are this tab's only
(start.js, sessionStorage); without JavaScript the list still reads as steps, and the paste block keeps its copy button."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "portal/static/start.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
BOXES = ("pre-access", "pre-uv", "pre-gh")


def test_the_prerequisites_are_a_checklist_that_reads_without_script(portal):
    text = portal.client().get("/").text
    for box in BOXES:
        assert f'<label for="{box}"><input type="checkbox" id="{box}" data-prereq>' in text
    assert '<div class="progress" hidden>' in text and 'src="/static/start.js"' in text
    assert '<progress id="start-progress" value="0" max="3" aria-labelledby="start-progress-label"></progress>' in text
    assert text.index("Before you start") < text.index("Paste this into your agent") < text.index("Register your app")
    assert 'data-copy="paste-block"' in text and '<pre id="paste-block">' in text and "Install <code>uv</code>" in text
    assert '<ol class="steps">' in text and '<a class="button gold" href="/register">Register an app</a>' in text


def call(name, *args):
    script = f"const m = require({json.dumps(str(JS))}); process.stdout.write(JSON.stringify(m.{name}(...{json.dumps(list(args))})));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@node
@pytest.mark.parametrize("saved, ticked", [('["pre-uv"]', ["pre-uv"]), ('["pre-uv", "pre-uv", "nope", 3]', ["pre-uv"]),
                                           ("not json", []), ('{"pre-uv": true}', []), ("", []), (None, [])])
def test_saved_ticks_are_read_defensively(saved, ticked):
    assert call("parseSaved", saved, list(BOXES)) == ticked


@node
def test_the_progress_label():
    assert call("progressLabel", 2, 3) == "2 of 3 done"


def test_the_script_never_writes_html_or_sends_anything():
    text = JS.read_text()
    for sink in ("inner" + "HTML", "outer" + "HTML", "document." + "write", "ev" + "al(", "fet" + "ch(", "XMLHttp" + "Request", "local" + "Storage"):
        assert sink not in text, sink
    assert "sessionStorage" in text
