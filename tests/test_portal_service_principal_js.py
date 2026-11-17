"""service_principal.js: the button says what will happen; the server decides (spec 2026-11-16 S3)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "portal/static/service_principal.js"
node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def call(name, *args):
    script = f"const m = require({json.dumps(str(JS))}); process.stdout.write(JSON.stringify(m.{name}(...{json.dumps(list(args))})));"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


@node
def test_the_label_says_create_or_request():
    assert call("buttonLabel", False) == "Create" and call("buttonLabel", True) == "Request"


def test_the_script_never_writes_html_or_runs_code():
    text = JS.read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\""):
        assert banned not in text, banned
