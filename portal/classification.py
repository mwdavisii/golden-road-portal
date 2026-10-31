"""The data classification levels (builder 0.5.0, spec 2026-10-30): public < confidential < protected, least to most sensitive.

This file is copied byte for byte to the portal (bootstrap/golden-road-portal/portal/classification.py): it imports only the standard
library, and tests/pipeline/test_portal_bootstrap.py keeps the two equal. The old levels are retired with no alias: a document that still names
one is refused with a message that names the replacement, so moving a record over is one edit."""

import re

LEVELS = ("public", "confidential", "protected")
RETIRED = {"internal": "confidential", "restricted": "confidential", "phi": "protected"}
# One line per level, shown wherever a person picks one. Placeholders until the policy's own words replace them.
MEANS = {"public": "anyone may see it", "confidential": "staff and the groups that need it",
         "protected": "regulated or highly sensitive data, named people only"}
# Where the data classification policy lives until configure.py records the real one (a mock: nothing is served there).
POLICY_PLACEHOLDER = "https://policies.example.com/data-classification"
# The one rule for that link, used by configure.py, the builder, the builder-guide schema (as ^...$) and the portal's setting: https, a
# host name with a dot, an optional port up to 65535, then an optional path, query and fragment ("#" is allowed). No user info,
# whitespace, control characters, quotes or angle brackets, and at most POLICY_LINK_MAX characters.
POLICY_LINK_PATTERN = (r"https://(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?::(?:6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[1-9][0-9]{0,3}))?"
                       r"(?:/[A-Za-z0-9._~%/?&=+#-]*)?")
POLICY_LINK_MAX = 2048


def policy_link_ok(value) -> bool:
    return isinstance(value, str) and len(value) <= POLICY_LINK_MAX and re.fullmatch(POLICY_LINK_PATTERN, value) is not None


def retired_message(value: str) -> str:
    return f"`{value}` is retired: use `{RETIRED[value]}`"


def retired_values(document) -> list[str]:
    """The retired levels a document names where a classification goes: a string value, or a map key, anywhere under a key whose
    name contains "classification" (data_classification, floor_classification, classification_tier, classification_asvs_level,
    sensitive_classifications, a routing rule's escalate.classifications). Each once, in the order found."""
    found, stack = [], [(document, False)]  # (node, under a classification key); a stack, not recursion: specs can nest deep
    while stack:
        node, under = stack.pop()
        if isinstance(node, dict):
            children = []
            for key, value in node.items():
                if under and isinstance(key, str) and key in RETIRED:
                    children.append((key, True))  # a map key: found as it is popped, before its value
                children.append((value, under or (isinstance(key, str) and "classification" in key)))
            stack.extend(reversed(children))
        elif isinstance(node, list):
            stack.extend((item, under) for item in reversed(node))
        elif under and isinstance(node, str) and node in RETIRED:
            found.append(node)
    return list(dict.fromkeys(found))


def retired_problems(document) -> list[str]:
    """One message per retired level the document names, for the reader to show before any schema error."""
    return [retired_message(value) for value in retired_values(document)]
