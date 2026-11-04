from pathlib import Path

PORTAL = Path(__file__).resolve().parents[1] / "portal"


def test_the_portal_imports_nothing_its_image_does_not_have():
    """The image installs requirements.txt only: no contracts, scorecard, builder or golden_road packages (decision D4)."""
    for path in PORTAL.rglob("*.py"):
        text = path.read_text()
        for name in ("contracts", "scorecard", "builder", "golden_road"):
            assert f"import {name}" not in text and f"from {name}" not in text, (path, name)


def test_the_web_imports_nothing_from_the_collector_and_no_private_setting_helper():
    """The web and the collector are separate processes: what both need (the outbox's note text and staleness) is in portal.outbox, and
    the settings helper they share is public (settings.https_url)."""
    import ast
    for path in PORTAL.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                if path.parent.name == "web":
                    assert "collectors" not in module, (path, module)
                assert not any(a.name.startswith("_") and module.endswith("settings") for a in node.names), (path, module)
