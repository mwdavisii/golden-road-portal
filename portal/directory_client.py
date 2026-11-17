""""Entra" (spec 2026-11-16 S9): a DirectoryClient makes a service principal with one call, create_principal(display_name, scopes) ->
(id, client_id). The only implementation is the mock: a new id (sp- and 24 hex digits, never one the mock directory lists) and a random
version 4 GUID for its client ID; the collector then records the row in the store, with the request created, in one transaction. A real
Microsoft Graph client (an app registration and its service principal, an app role assignment per scope, and admin consent) is a later
step; it must look the name up first, so a pass that stopped after the call never makes a second principal."""

import secrets
import uuid

from .store.base import CLIENT_ID, PRINCIPAL_ID, SCOPE_ID, SP_NAME


class DirectoryClientError(Exception):
    pass


def reserved_from(directory) -> frozenset:
    """The ids and client IDs a directory lists: a created principal never takes one (decided while planning: item 19)."""
    return frozenset(p.id for p in directory.principals) | frozenset(p.client_id for p in directory.principals)


class MockDirectoryClient:
    name = "mock-directory"

    def __init__(self, reserved=frozenset(), *, token_hex=secrets.token_hex, new_guid=uuid.uuid4):
        self.reserved = frozenset(reserved)
        self._token_hex, self._new_guid = token_hex, new_guid

    def create_principal(self, display_name, scopes) -> tuple[str, str]:
        if not isinstance(display_name, str) or not SP_NAME.fullmatch(display_name):
            raise DirectoryClientError("a service principal's name is sp- and then lowercase letters, digits and -")
        if not scopes or not all(isinstance(s, str) and SCOPE_ID.fullmatch(s) for s in scopes):
            raise DirectoryClientError("a service principal is made with at least one Graph application permission")
        while True:  # a clash with a listed entry is drawn again: 96 random bits, so in practice never
            principal_id, client_id = f"sp-{self._token_hex(12)}", str(self._new_guid())
            if not PRINCIPAL_ID.fullmatch(principal_id) or not CLIENT_ID.fullmatch(client_id):
                raise DirectoryClientError("the mock directory made an id or a client ID of the wrong shape")
            if principal_id not in self.reserved and client_id not in self.reserved:
                return principal_id, client_id
