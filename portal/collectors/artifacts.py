"""A re-review event out of a scorecard artifact (spec 2026-10-31 R4, R10). The zip is held in memory; only its entry named exactly
re-review-event.json is read, within a size cap, then parsed as strict JSON (no NaN, no key twice) and validated against re-review-event/v1.
Nothing is extracted, and no name from the zip is used as a path: a name is only compared. The outcome of anything else is a word, counted
by the collector and never shown."""

import io
import json
import re
import zipfile

from ..contracts import validate

ARTIFACT_MAX_BYTES = 16 * 1024 * 1024  # the whole zip (scorecard.json, the comment, the event): bounds memory, not work
EVENT_MAX_BYTES = 1024 * 1024  # 40 signals of 4000 characters each, escaped, fit with room to spare
EVENT_NAME = "re-review-event.json"
MAX_ENTRIES = 10000  # an artifact holds a handful: the zip's own count and directory size, read before it is opened, then recounted
DIRECTORY_BYTES_PER_ENTRY = 100  # the central directory may take at most MAX_ENTRIES * this many bytes
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # no string of an event carries one (re-review-event/v1's summary pattern, and here for all)


def _too_big(data: bytes) -> bool:
    """What the end of the zip says before it is opened: no end record, a ZIP64 end locator (no artifact needs one), more entries than
    MAX_ENTRIES, or a central directory larger than that many entries take. The count can be patched, so the entries are counted again
    once it is open."""
    at = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if at < 0 or len(data) < at + 22 or data[max(0, at - 20):max(0, at - 16)] == b"PK\x06\x07":  # a ZIP64 end locator sits right before it
        return True
    count, size = int.from_bytes(data[at + 10:at + 12], "little"), int.from_bytes(data[at + 12:at + 16], "little")
    return count >= 0xFFFF or count > MAX_ENTRIES or size > MAX_ENTRIES * DIRECTORY_BYTES_PER_ENTRY


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def _unique(pairs):
    if len({k for k, _ in pairs}) != len(pairs):
        raise ValueError("a key twice")
    return dict(pairs)


def event_from_zip(data: bytes):
    """The validated event (a dict), or why there is none: no_event, too_large or invalid."""
    if len(data) > ARTIFACT_MAX_BYTES:
        return "too_large"
    if _too_big(data):
        return "invalid"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if len(zf.infolist()) > MAX_ENTRIES:  # the end record's count understated them
                return "invalid"
            named = [info for info in zf.infolist() if info.filename == EVENT_NAME]
            if not named:
                return "no_event"
            if len(named) > 1:
                return "invalid"
            info = named[0]
            if info.is_dir() or info.flag_bits & 0x1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                return "invalid"
            if info.file_size > EVENT_MAX_BYTES:
                return "too_large"
            with zf.open(info) as entry:
                raw = entry.read(EVENT_MAX_BYTES + 1)  # the header's size may lie: never read more than the cap
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, ValueError):
        return "invalid"
    if len(raw) > EVENT_MAX_BYTES:
        return "too_large"
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constant, object_pairs_hook=_unique)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "invalid"
    if validate("re-review-event.v1", doc) or any(CONTROL.search(s) for s in _strings(doc)):
        return "invalid"
    return doc