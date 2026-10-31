"""The scorecard artifact (spec 2026-10-31 R4, R10): downloaded through GitHub's redirect without the token, capped, read in memory, one
entry only, validated."""

import io
import json
import os
import zipfile

import httpx
import pytest
from portal_fakes import READ_WITH_ACTIONS, FakeGitHub, FakeStorage, transport, zipped

from portal.collectors.artifacts import EVENT_MAX_BYTES, event_from_zip
from portal.github.client import GitHub, GitHubError, TooLarge

ZIP = "/repos/acme/scorecard-factory/actions/artifacts/55/zip"
HOSTS = ("*.storage.example.test",)  # the fake storage's host; the collector's default is *.blob.core.windows.net
EVENT = {"schema": "re-review-event/v1", "repo": "acme/notes", "pr": 7, "head_sha": "a" * 40, "tree_sha": "b" * 40,
         "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z", "level": "elevated", "floor": "standard", "complete": True,
         "signals": [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "resources_tier raised", "count": 1}]}


def wired(answer_for_zip, storage=None):
    gh, storage = FakeGitHub(permissions=READ_WITH_ACTIONS), storage or FakeStorage()
    gh.on("GET", ZIP, answer_for_zip)
    return gh, storage, GitHub("https://api.github.com", httpx.Client(transport=transport(gh, storage)), lambda: "ghs_" + "r" * 36)


def test_the_zip_comes_from_the_redirect_without_the_token():
    storage = FakeStorage()
    url = storage.put("blob/55.zip", b"PK-bytes")
    gh, storage, client = wired((302, {}, {"location": url}), storage)
    assert client.download(ZIP, cap=1024, hosts=HOSTS) == b"PK-bytes"
    assert gh.calls[0]["headers"]["authorization"].startswith("Bearer ")
    assert "authorization" not in storage.calls[0]["headers"]


@pytest.mark.parametrize("answer", [(200, {"not": "a redirect"}), (302, {}, {"location": "http://results.storage.example.test/x"}),
                                    (302, {}, {"location": "https://user:pw@results.storage.example.test/x"}), (302, {}, {}),
                                    (404, {"message": "gone"})])
def test_anything_but_an_https_redirect_is_refused_and_the_url_is_never_repeated(answer):
    from portal.github.client import HostRefused
    _, _, client = wired(answer)
    with pytest.raises(GitHubError) as exc:
        client.download(ZIP, cap=1024, hosts=HOSTS)
    assert "storage.example.test" not in str(exc.value) and "pw" not in str(exc.value)
    assert isinstance(exc.value, HostRefused) == (answer[0] == 302)   # a redirect the collector won't follow, versus a refusal


def test_storage_that_redirects_again_or_fails_is_refused():
    storage = FakeStorage()
    again = storage.put("blob/again.zip", b"", status=302, headers={"location": "https://elsewhere.example.test/x"})
    _, storage, client = wired((302, {}, {"location": again}), storage)
    with pytest.raises(GitHubError) as exc:
        client.download(ZIP, cap=1024, hosts=HOSTS)
    assert "sig=" not in str(exc.value)


def test_more_than_the_cap_is_too_large():
    storage = FakeStorage()
    url = storage.put("blob/big.zip", b"x" * 2048)
    _, _, client = wired((302, {}, {"location": url}), storage)
    with pytest.raises(TooLarge):
        client.download(ZIP, cap=1024, hosts=HOSTS)


def test_the_event_is_read_from_its_one_entry():
    data = zipped({"scorecard.json": b"{}", "re-review-event.json": json.dumps(EVENT).encode(), "comment.md": b"# hi"})
    assert event_from_zip(data) == EVENT


@pytest.mark.parametrize("entries, outcome", [
    ({"scorecard.json": b"{}"}, "no_event"),
    ({"out/re-review-event.json": json.dumps(EVENT).encode()}, "no_event"),
    ({"../re-review-event.json": json.dumps(EVENT).encode()}, "no_event"),
    ({"re-review-event.json": b"x" * (EVENT_MAX_BYTES + 1)}, "too_large"),
    ({"re-review-event.json": b"{not json"}, "invalid"),
    ({"re-review-event.json": b'{"schema": "re-review-event/v1", "schema": "re-review-event/v1"}'}, "invalid"),
    ({"re-review-event.json": json.dumps({**EVENT, "pr": float("nan")}).encode()}, "invalid"),
    ({"re-review-event.json": json.dumps({**EVENT, "level": "high"}).encode()}, "invalid"),
    ({"re-review-event.json": json.dumps({**EVENT, "signals": [{"id": "T-17", "kind": "trigger", "level": "elevated",
                                                               "summary": "<script>", "count": 1, "ref": "src/app.py"}]}).encode()}, "invalid")])
def test_anything_else_is_counted_never_read_further(entries, outcome):
    assert event_from_zip(zipped(entries)) == outcome


def test_two_entries_of_that_name_or_a_broken_zip_are_invalid():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("re-review-event.json", json.dumps(EVENT))
        with pytest.warns(UserWarning):
            zf.writestr("re-review-event.json", json.dumps(EVENT))
    assert event_from_zip(out.getvalue()) == "invalid"
    assert event_from_zip(b"PK\x03\x04 not a zip") == "invalid"


def test_nothing_is_ever_extracted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in ("extract", "extractall"):
        monkeypatch.setattr(zipfile.ZipFile, name, lambda *a, **k: pytest.fail("extracted"))
    assert event_from_zip(zipped({"re-review-event.json": json.dumps(EVENT).encode()})) == EVENT
    assert os.listdir(tmp_path) == []


# ---- more cases ----

def event_zip(**changes):
    return zipped({"re-review-event.json": json.dumps({**EVENT, **changes}).encode()})


@pytest.mark.parametrize("changes", [{"created": "2026-02-30T12:00:00Z"}, {"created": "2026-10-31T24:00:00Z"},
                                     {"signals": [{**EVENT["signals"][0], "summary": "raised\u0000"}]},
                                     {"signals": [{**EVENT["signals"][0], "summary": "two\nlines"}]},
                                     {"signals": [{**EVENT["signals"][0], "summary": "c1 \u0085 control"}]}])
def test_an_impossible_time_or_a_control_character_is_invalid(changes):
    assert event_from_zip(event_zip(**changes)) == "invalid"


def test_a_zip_with_too_many_entries_is_refused_before_it_is_opened(monkeypatch):
    data = zipped({**{f"f{i}": b"" for i in range(10001)}, "re-review-event.json": json.dumps(EVENT).encode()})
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: pytest.fail("opened"))
    assert event_from_zip(data) == "invalid"


def test_the_download_goes_only_to_an_allowed_storage_host():
    storage = FakeStorage()
    url = storage.put("blob/55.zip", b"PK-bytes")
    _, _, client = wired((302, {}, {"location": url}), storage)
    with pytest.raises(GitHubError) as exc:
        client.download(ZIP, cap=1024, hosts=("*.blob.core.windows.net",))  # not the fake's host
    assert storage.calls == [] and "storage.example.test" not in str(exc.value)
    assert client.download(ZIP, cap=1024, hosts=("*.storage.example.test",)) == b"PK-bytes"


def test_the_download_asks_for_the_bytes_as_stored_and_refuses_an_encoded_answer():
    storage = FakeStorage()
    url = storage.put("blob/55.zip", b"PK-bytes", headers={"content-encoding": "gzip"})
    _, storage, client = wired((302, {}, {"location": url}), storage)
    with pytest.raises(GitHubError):
        client.download(ZIP, cap=1024, hosts=HOSTS)
    assert storage.calls[0]["headers"]["accept-encoding"] == "identity"


def test_a_download_slower_than_its_deadline_stops(monkeypatch):
    import portal.github.client as client_module
    storage = FakeStorage()
    url = storage.put("blob/55.zip", b"PK-bytes")
    _, _, client = wired((302, {}, {"location": url}), storage)
    import itertools
    ticks = itertools.chain([0.0], itertools.repeat(500.0))  # the start, then every later reading: past the deadline
    monkeypatch.setattr(client_module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(GitHubError) as exc:
        client.download(ZIP, cap=1024, hosts=HOSTS, deadline=120)
    assert exc.value.status == 0  # transient: the next pass tries again


# ---- the entry cap can't be talked around ----

def patched(entries, count):
    """A zip of `entries` empty files whose end-of-central-directory record claims `count` entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(entries):
            zf.writestr(f"{i}", b"")
        zf.writestr("re-review-event.json", json.dumps(EVENT))
    data = bytearray(buf.getvalue())
    at = data.rfind(b"PK\x05\x06")
    data[at + 8:at + 10] = count.to_bytes(2, "little")
    data[at + 10:at + 12] = count.to_bytes(2, "little")
    return bytes(data)


def test_a_zip_that_understates_its_entries_is_refused():
    assert event_from_zip(patched(20000, 1)) == "invalid"


def test_a_zip_with_a_zip64_end_locator_is_refused_and_only_where_one_sits():
    data = zipped({"re-review-event.json": json.dumps(EVENT).encode()})
    at = data.rfind(b"PK\x05\x06")
    locator = b"PK\x06\x07" + b"\x00" * 16   # 20 bytes: its signature sits at data[at - 20:at - 16]
    assert event_from_zip(data[:at] + locator + data[at:]) == "invalid"
    inside = zipped({"re-review-event.json": json.dumps({**EVENT, "tree_sha": "b" * 40}).encode(), "note.txt": b"PK\x06\x07 in a file"})
    assert isinstance(event_from_zip(inside), dict)   # the signature inside a file is just bytes


def test_a_central_directory_too_big_for_its_count_is_refused(monkeypatch):
    import portal.collectors.artifacts as artifacts
    monkeypatch.setattr(artifacts, "MAX_ENTRIES", 2)   # 2 entries may take at most 200 bytes of directory
    data = zipped({"a" * 150: b"", "re-review-event.json": json.dumps(EVENT).encode()})
    assert event_from_zip(data) == "invalid"
