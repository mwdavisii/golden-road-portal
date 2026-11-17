"""The directory client (spec 2026-11-16 S9): create_principal(display_name, scopes) -> (id, client_id). The only one is the mock."""

import uuid

import pytest

from portal.directory import load_mock
from portal.directory_client import DirectoryClientError, MockDirectoryClient, reserved_from
from portal.store.base import CLIENT_ID, PRINCIPAL_ID


def test_a_principal_gets_a_new_id_and_a_random_v4_client_id():
    client = MockDirectoryClient()
    first, second = client.create_principal("sp-notes-rooms", ("Calendars.Read",)), client.create_principal("sp-notes-x", ("Mail.Send",))
    assert PRINCIPAL_ID.fullmatch(first[0]) and CLIENT_ID.fullmatch(first[1]) and first != second


def test_an_id_or_client_id_the_mock_directory_lists_is_never_given():
    reserved = reserved_from(load_mock())
    assert "sp-hello-app" in reserved and "11111111-1111-4111-8111-111111111111" in reserved
    tokens = iter(["0" * 24, "1" * 24])
    guids = iter([uuid.UUID("11111111-1111-4111-8111-111111111111"), uuid.UUID("0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f")])
    client = MockDirectoryClient(frozenset({"sp-" + "0" * 24}) | reserved, token_hex=lambda n: next(tokens), new_guid=lambda: next(guids))
    assert client.create_principal("sp-notes-rooms", ("Calendars.Read",)) == ("sp-" + "1" * 24, "0e2f6c1d-3b4a-4c5d-8e6f-7a8b9c0d1e2f")


@pytest.mark.parametrize("name, scopes", [("SP-notes", ("Calendars.Read",)), ("sp-", ("Calendars.Read",)), ("sp-notes", ()),
                                          ("sp-notes", ("calendars.read",))])
def test_a_bad_principal_is_refused(name, scopes):
    with pytest.raises(DirectoryClientError):
        MockDirectoryClient().create_principal(name, scopes)


def test_a_generator_that_gives_a_bad_id_is_an_error_not_a_loop():
    with pytest.raises(DirectoryClientError):
        MockDirectoryClient(token_hex=lambda n: "XYZ").create_principal("sp-notes-rooms", ("Calendars.Read",))
