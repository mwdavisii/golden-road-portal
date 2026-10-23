"""The image's entry point (builder-spec.json: portal.main:app, wrapped by the generated _platform/entry.py)."""

import os

from portal.web.app import create_app_from_env

app = create_app_from_env(os.environ)
