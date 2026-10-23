"""Portal IDs: a kind (p person, r request, l identity link), an underscore and 32 hex digits."""

import secrets


def new_id(kind: str) -> str:
    return f"{kind}_{secrets.token_hex(16)}"
