"""The shapes of values taken from a GitHub answer, checked before they are used or stored."""

import re

BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
SHA = re.compile(r"[0-9a-f]{40}")
