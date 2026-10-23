"""The image's entry point (builder-spec.json: portal.main:app, wrapped by the generated _platform/entry.py)."""

from portal.web.app import create_app

app = create_app()
