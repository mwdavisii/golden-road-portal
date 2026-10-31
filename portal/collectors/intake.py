"""The factory's intake records as the portal reads them (spec 2026-10-31 C4 and R2), through the read App, with the scorecard's own rules:
safe YAML without duplicate keys, implicit timestamps, anchors or aliases, a retired level refused, a valid intake/v1 or intake/v2 for
this very repo, nobody approving their own registration, a floor that holds inside the record, a real review date, and the central mark
spelled as the one line scorecard-assess greps. What is kept is a small view, never more than the approval screen and the re-review
list show; anything else is {"schema": "invalid"}."""

import datetime as dt
import re

import yaml

from ..classification import retired_values
from ..contracts import validate
from ..floor import below_floor
from ..github.client import GitHubError
from ..rereview import auto_close_note, date_reason
from ..store.base import DATE_REASONS, Conflict, IntakeObserved
from ..writer.allowlist import factory, intake_path
from .repo_files import (REGULAR, UNREADABLE, Malformed, Transient, case_variants, git_tree, head_commit, is_transient, read_blob,
                         read_with_sha)
from .rules import StrictLoader

RECORD_CAP = 65536  # a record is a few hundred bytes; anything near this is not one
PORTAL_HEADER = re.compile(r"# Written by the golden road portal \(request r_[0-9a-f]{32}\)\.")  # writer.render.HEADER's first line
VIEW_FIELDS = ("tier", "data_classification", "purpose", "owner", "review_by", "scorecard")
MAX_OWNER = 200  # intake/v1's owner is free text; a longer one isn't a team name the pages and the PR body should carry
FLOOR_FIELDS = ("evidence_commit", "floor_tier", "floor_classification")
INVALID = {"schema": "invalid"}


class RecordUnreadable(Exception):
    """The record is there, but there is no blob SHA to compare against: a refusal, a directory, an answer of the wrong shape."""


class RecordCaseMismatch(Exception):
    """The factory holds the record under another spelling of the repo's path (as well, or instead): which one is the record?"""


CENTRAL_LINE = "scorecard: central"  # the one line scorecard-assess greps (grep -qxF: lines end at \n only, nothing else is stripped)


def _lines(text) -> list[str]:
    """The lines as GNU grep reads them: split at \n only, so "scorecard: central\r" (CRLF) is not the line, and U+2028 (which ends a
    line for YAML and str.splitlines) doesn't start one."""
    return text.split("\n")


def _invalid(text) -> dict:
    """INVALID, keeping only a central mark spelled as the poller reads it: the approval carries it, so replacing a
    broken record of a central app doesn't take the app out of central review."""
    return {**INVALID, "scorecard": "central"} if text is not None and CENTRAL_LINE in _lines(text) else dict(INVALID)


def _floor_holds(doc) -> bool:
    """src/scorecard/config.py's _check_floor: all three floor fields or none; below the floor only with the approver's override, made
    at the approval; no override otherwise."""
    present = [f for f in FLOOR_FIELDS if f in doc]
    override = doc.get("floor_override")
    if not present:
        return override is None
    if len(present) != len(FLOOR_FIELDS):
        return False
    below = bool(below_floor(doc["tier"], doc["data_classification"], doc["floor_tier"], doc["floor_classification"]))
    if override is None:
        return not below
    return below and override["by"] == doc["approved_by"] and override["at"] == doc["approved_at"]


def parse_record(raw, repo) -> dict:
    """The view of an intake record for `repo`: {schema, by_portal, tier, data_classification, purpose, owner, review_by?, scorecard?},
    or INVALID."""
    if raw is UNREADABLE:
        return dict(INVALID)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return dict(INVALID)
    try:
        doc = yaml.load(text, Loader=StrictLoader)  # noqa: S506 - StrictLoader is a SafeLoader
    except (yaml.YAMLError, RecursionError):
        return _invalid(text)
    if not isinstance(doc, dict) or retired_values(doc):
        return _invalid(text)
    name = {"intake/v1": "intake.v1", "intake/v2": "intake.v2"}.get(doc.get("schema"))
    if name is None or validate(name, doc) or doc["repo"] != repo or len(doc["owner"]) > MAX_OWNER:
        return _invalid(text)
    if name == "intake.v2":
        try:
            dt.date.fromisoformat(doc["review_by"])
        except ValueError:
            return _invalid(text)
        if doc["approved_by"] == doc["registered_by"] or not _floor_holds(doc):
            return _invalid(text)
    if doc.get("scorecard") == "central" and CENTRAL_LINE not in _lines(text):
        return dict(INVALID)
    return {"schema": doc["schema"], "by_portal": bool(PORTAL_HEADER.match(text)), **{k: doc[k] for k in VIEW_FIELDS if k in doc}}


def current_record(gh, factory, repo, commit):
    """(blob SHA, view) of `repo`'s intake record in the factory at `commit`, or (None, None) when it has none (a first registration).
    Raises Transient when GitHub can't answer for now, RecordUnreadable when the file can't be compared, and RecordCaseMismatch when
    the factory also (or only) holds it under another spelling."""
    try:
        if case_variants(gh, factory, intake_path(repo), commit):
            raise RecordCaseMismatch()
        found = read_with_sha(gh, factory, intake_path(repo), commit, RECORD_CAP)
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise RecordUnreadable() from None
    except Malformed:
        raise RecordUnreadable() from None
    if found is None:
        return None, None
    sha, raw = found
    return sha, parse_record(raw, repo)


# ---- observing every record (spec 2026-10-31 R2, R3) ----

NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _tree(gh, repo, sha) -> list[dict]:
    """repo_files.git_tree, with GitHub busy as Transient."""
    try:
        return git_tree(gh, repo, sha)
    except GitHubError as exc:
        if is_transient(exc):
            raise Transient(str(exc)) from exc
        raise


def _row(repo, sha, problem=None, view=None) -> IntakeObserved:
    """A cache row: the view of a valid record, or a problem. An invalid record keeps only a central mark spelled as the poller reads
    it (parse_record's rule), as the approval of its replacement does."""
    if view is None or view["schema"] == "invalid":
        return IntakeObserved(repo, sha, None, False, None, None, None, None, None, (view or {}).get("scorecard"), problem or "invalid")
    review = dt.date.fromisoformat(view["review_by"]) if "review_by" in view else None
    return IntakeObserved(repo, sha, view["schema"], view["by_portal"], view["owner"], view["tier"], view["data_classification"],
                          view["purpose"], review, view.get("scorecard"), None)


def observe(gh, factory_repo, commit, cached) -> list[IntakeObserved]:
    """Every intake/<owner>/<repo>.yaml at `commit`, as rows. A record whose blob is the one `cached` holds is not fetched again; a symlink
    or a submodule is not_a_file and is never followed; a repo named twice in different case is case_mismatch (as the evidence and the
    writer refuse it: which file is the record?), in the first spelling of the sorted walk."""
    root = {e["path"]: e for e in _tree(gh, factory_repo, commit)}
    intake = root.get("intake")
    if intake is None or intake["type"] != "tree":
        return []
    found = []  # (repo, entry) in the sorted walk
    for owner in sorted((e for e in _tree(gh, factory_repo, intake["sha"])
                         if e["type"] == "tree" and NAME.fullmatch(e["path"]) and e["path"] not in (".", "..")), key=lambda e: e["path"]):
        for entry in sorted(_tree(gh, factory_repo, owner["sha"]), key=lambda e: e["path"]):
            name = entry["path"][:-5] if entry["path"].endswith(".yaml") else ""
            if NAME.fullmatch(name) and name not in (".", "..") and entry["type"] in ("blob", "commit"):
                found.append((f"{owner['path']}/{name}", entry))
    spellings = {}
    for repo, _ in found:
        spellings[repo.lower()] = spellings.get(repo.lower(), 0) + 1
    rows, seen = [], set()
    for repo, entry in found:
        if repo.lower() in seen:
            continue
        seen.add(repo.lower())
        if spellings[repo.lower()] > 1:
            rows.append(_row(repo, entry["sha"], "case_mismatch"))
        elif entry["type"] != "blob" or entry["mode"] not in REGULAR:
            rows.append(_row(repo, entry["sha"], "not_a_file"))
        elif (old := cached.get(repo)) is not None and old.blob_sha == entry["sha"] and old.problem is None:  # a problem row is read again
            rows.append(old)
        else:
            body = read_blob(gh, factory_repo, entry["sha"], RECORD_CAP)
            rows.append(_row(repo, entry["sha"], "unreadable") if body is UNREADABLE else _row(repo, entry["sha"], view=parse_record(body, repo)))
    return rows


def sync_date_items(store, rows, *, today, due_soon_days, now) -> tuple[int, int]:
    """(opened, closed): each readable intake/v2 record gets the date reason its review_by gives today (spec R3); an open date item whose
    record no longer gives it, with that date, closes by itself. Kept as they are: the items of a repo whose record can't be read this
    pass or was rewritten as intake/v1 (a broken or older file must not clear an overdue review); collect_intake doesn't call this at
    all when every record is gone at once. A closed item never reopens for the same date (the store refuses: item_exists)."""
    by_repo = {r.repo.lower(): r for r in rows}
    held = {key for key, r in by_repo.items() if r.problem is not None or r.schema == "intake/v1"}
    wanted = {}
    for key, r in by_repo.items():
        reason = date_reason(r.review_by, today, due_soon_days) if r.problem is None and r.schema == "intake/v2" else None
        if reason:
            wanted[(key, reason)] = (r.repo, r.review_by.isoformat())
    opened = closed = 0
    still_open = set()
    for item in store.rereview_items():
        if item.reason not in DATE_REASONS or item.repo.lower() in held:
            continue
        want = wanted.get((item.repo.lower(), item.reason))
        if want is not None and want[1] == item.key:
            still_open.add((item.repo.lower(), item.reason))
            continue
        try:
            store.auto_close_rereview_item(item_id=item.id, note=auto_close_note(by_repo.get(item.repo.lower()), item, today, due_soon_days), now=now)
            closed += 1
        except Conflict:
            continue  # closed meanwhile by an admin
    for (key, reason), (repo, date) in sorted(wanted.items()):
        if (key, reason) in still_open:
            continue
        try:
            store.open_rereview_item(repo=repo, reason=reason, key=date, now=now)
            opened += 1
        except Conflict:
            continue  # this date's item was closed with a note: it never reopens
    return opened, closed


def collect_intake(gh, store, *, org, today, due_soon_days, now) -> dict:
    """One pass (spec R2, R3): observe every intake record on the factory's default branch, replace the cache, then open and close the date
    items. GitHub busy (or an answer of the wrong shape): nothing changes. The factory unreadable: nothing changes, and the Re-review page
    says so."""
    repo = factory(org)
    cached = {r.repo: r for r in store.intake_observed()}
    try:
        commit = head_commit(gh, repo)
        rows = observe(gh, repo, commit, cached)
    except (Transient, Malformed):
        return {"transient": True}
    except GitHubError:
        store.set_rereview_status(source="intake", problem="factory_unreadable", counts={}, now=now)
        return {"problem": "factory_unreadable"}
    if not rows and cached:  # every record gone at once: keep the cache and every item until a record is there again
        store.set_rereview_status(source="intake", problem=None, counts={"records": 0, "unreadable": 0, "held": len(cached)}, now=now)
        return {"records": 0, "unreadable": 0, "opened": 0, "closed": 0, "held": len(cached)}
    store.replace_intake_observed(rows, observed_at=now)
    opened, closed = sync_date_items(store, rows, today=today, due_soon_days=due_soon_days, now=now)
    counts = {"records": len(rows), "unreadable": sum(1 for r in rows if r.problem)}
    store.set_rereview_status(source="intake", problem=None, counts=counts, now=now)
    return {**counts, "opened": opened, "closed": closed}
