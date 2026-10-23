from pathlib import Path

PORTAL = Path(__file__).resolve().parents[1] / "portal"


def test_the_portal_imports_nothing_its_image_does_not_have():
    """The image installs requirements.txt only: no contracts, scorecard, builder or golden_road packages (decision D4)."""
    for path in PORTAL.rglob("*.py"):
        text = path.read_text()
        for name in ("contracts", "scorecard", "builder", "golden_road"):
            assert f"import {name}" not in text and f"from {name}" not in text, (path, name)
