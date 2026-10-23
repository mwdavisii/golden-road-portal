"""Portal roles from the OIDC groups claim (P15). Contributor is the default for any signed-in person and isn't listed."""

ROLE_NAMES = ("admin", "ops")


def roles_for(groups, role_groups: dict) -> tuple[str, ...]:
    have = {g for g in groups if isinstance(g, str)}
    return tuple(sorted(role for role, wanted in role_groups.items() if have & wanted))
