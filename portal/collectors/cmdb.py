"""The collector's CMDB part (spec 2026-10-31 N1-N4): every intake record the factory holds is a business application in ServiceNow.
Each pass sends each readable record that is new, whose synced fields or intake file changed since the last good sync, or whose last
attempt failed, through the Identification and Reconciliation API (identified by correlation_id), and records the CI's sys_id and number
or the error.
The intake file in git stays the truth; ServiceNow follows it, never the other way (N3). An intake/v2 record whose owner matches no
single ServiceNow user is never made a CI (a CI without its owner is an orphaned app): it is cmdb_owner_unknown and retried every pass.
A CI the portal made already still follows the app's lifecycle when its owner leaves (the final review: I-2): while its lifecycle fields
read otherwise than the portal's, it is reconciled without owned_by (ServiceNow keeps the owner it has), and stays cmdb_owner_unknown. When
ServiceNow can't be reached (no answer, a 5xx, or a 4xx about the integration itself: PASS_STOPPING) the pass stops there; the rest
wait for the next pass, and the part's status says so. Any other 4xx is about the one CI (a CI deleted in ServiceNow answers 404): that
row is cmdb_refused, the pass goes on, and the next pass reconciles it again.
0.7.0 (spec 2026-11-02 "ServiceNow write-back", rule 9): the CI's lifecycle fields follow the portal's lifecycle_state, and are part of the
hash (a state change re-sends the CI). For a CI otherwise unchanged, the pass reads the lifecycle fields it reports: one a person changed
in ServiceNow is written back, the write-back read again, and noted on the task of the request that set the state (an app never acted on
has no task: no note). A field the CI doesn't report is not an edit (decided while planning: item 12). The note names the fields, never
the values ServiceNow holds (ci_lifecycle passes only each field's own values, and the note says the portal's)."""

import hashlib
import json

from ..servicenow import ServiceNowError, email_ok

# The CI's lifecycle fields for each app state (spec 2026-11-02 "ServiceNow write-back"): ServiceNow's choice values, as strings.
LIFECYCLE_CI = {"active": {"operational_status": "1", "install_status": "1", "u_golden_road_state": "active"},
                "suspended": {"operational_status": "2", "install_status": "1", "u_golden_road_state": "suspended"},
                "decommissioned": {"operational_status": "6", "install_status": "7", "u_golden_road_state": "decommissioned"}}
# 4xx answers about the integration itself, not one CI (its credentials, a proxy's, a rate limit): they stop the pass as an outage does
PASS_STOPPING = (401, 407, 429)
SHORT_MAX = 1000  # short_description: ServiceNow's field is shorter than a purpose (confirm against the instance)


def _short(purpose: str) -> str:
    return purpose if len(purpose) <= SHORT_MAX else purpose[:SHORT_MAX - 3] + "..."


def _ci_refusal(exc: ServiceNowError) -> bool:
    """A 4xx about one CI (a CI deleted in ServiceNow is a 404, an ACL on it a 403), not about the integration (PASS_STOPPING)."""
    return exc.status is not None and 400 <= exc.status < 500 and exc.status not in PASS_STOPPING


def correlation_id(repo: str) -> str:
    """Lowercase: GitHub names are case-insensitive, so a rename that only changes case updates the same CI."""
    return f"github:{repo.lower()}"


def lifecycle_fields(state) -> dict:
    """The four fields for the app's LifecycleState (None: active, never acted on); u_golden_road_task is the task, or empty when active."""
    name = state.state if state is not None else "active"
    return {**LIFECYCLE_CI[name], "u_golden_road_task": state.task if state is not None and state.task else ""}


def edited_fields(reported, want) -> list:
    """The lifecycle fields the CI reports with another value than the portal's, by name (rule 9)."""
    return sorted(k for k, v in reported.items() if want.get(k) != v)


def ci_values(row, owner_sys_id, github_web, lifecycle=None) -> dict:
    """N2's fields for one observed record, and the lifecycle fields of the app's state (lifecycle: its LifecycleState, or None).
    owned_by only for a record with an owner of record (intake/v2)."""
    values = {"name": row.repo, "short_description": _short(row.purpose), "u_tier": row.tier,
              "u_data_classification": row.data_classification, "u_repo_url": f"{github_web}/{row.repo}",
              "correlation_id": correlation_id(row.repo), "discovery_source": "golden_road"}
    if row.review_by is not None:
        values["u_review_by"] = row.review_by.isoformat()
    if owner_sys_id is not None:
        values["owned_by"] = owner_sys_id
    values.update(lifecycle_fields(lifecycle))
    return values


def fields_hash(row, owner_email, owner_sys_id, github_web, lifecycle=None) -> str:
    """What decides a re-send: N2's inputs (the repo URL's host among them), the owner's email (a new email may match a ServiceNow user),
    the user it matches now (another user may take the email over, and owned_by must follow), and the intake file's blob: each new file
    (a renewal with the same values among them) is reconciled once, ServiceNow answers NO_CHANGE, and last_synced_at follows the merge. And
the lifecycle fields: a state change re-sends the CI (the first pass after 0.7.0 updates every CI once, with the active fields)."""
    doc = {"repo": row.repo, "blob_sha": row.blob_sha, "schema": row.schema,
           "purpose": row.purpose, "tier": row.tier, "classification": row.data_classification,
           "review_by": row.review_by.isoformat() if row.review_by else None, "owner_email": (owner_email or "").lower(),
           "owner_sys_id": owner_sys_id, "github_web": github_web, "lifecycle": lifecycle_fields(lifecycle)}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _orphan_edits(client, kept, want) -> list:
    """The lifecycle fields that read otherwise than the portal's on the CI of a record whose owner matches no ServiceNow user: [] when
    the portal never made its CI, or its CI is gone from ServiceNow (no CI is made again without its owner)."""
    if kept is None or kept.sys_id is None:
        return []
    try:
        return edited_fields(client.ci_lifecycle(kept.sys_id), want)
    except ServiceNowError as exc:
        if not _ci_refusal(exc):
            raise
        return []


def seed_mock(servicenow, store) -> None:
    """At collector start: the mock's CI table from the CIs the portal recorded, so a restart keeps their sys_ids and numbers. The
    portal keeps no copy of the values it sent, so each is seeded without them."""
    servicenow.seed([(correlation_id(r.repo), r.sys_id, r.number, None) for r in store.cmdb_records() if r.sys_id is not None])


def collect_cmdb(store, servicenow, *, github_web, now) -> dict:
    """One pass. Returns the counts (synced, unchanged, owner_unknown, refused, unreachable, corrected: the synced CIs whose lifecycle
    fields were written back over an edit in ServiceNow) and the part's problem, which it also records as the cmdb part's status."""
    rows = [r for r in store.intake_observed() if r.problem is None]
    records = {r.repo.lower(): r for r in store.cmdb_records()}
    states = {s.repo.lower(): s for s in store.lifecycle_states()}
    counts = {"synced": 0, "unchanged": 0, "owner_unknown": 0, "refused": 0, "unreachable": 0, "corrected": 0}
    with servicenow.client() as client:
        for row in rows:
            email = store.person_email(row.owner) if row.schema == "intake/v2" else None
            state = states.get(row.repo.lower())
            want = lifecycle_fields(state)
            try:
                # an owner of record's user is looked up every pass: the hash holds who it is now
                owner = client.user_sys_id(email) if row.schema == "intake/v2" and email_ok(email) else None
                digest = fields_hash(row, email, owner, github_web, state)
                kept = records.get(row.repo.lower())
                edited, orphan = [], row.schema == "intake/v2" and owner is None
                if orphan:
                    edited = _orphan_edits(client, kept, want)
                    if not edited:
                        store.record_cmdb_error(repo=row.repo, error="cmdb_owner_unknown", now=now)
                        counts["owner_unknown"] += 1
                        continue
                # a refused record is read too: a write-back the instance didn't keep is still an edit, so the next pass reads it again
                # and refuses it again (never synced, never noted); one that reads right is sent again, and a good sync clears the error
                elif kept is not None and kept.fields_hash == digest and kept.last_error in (None, "cmdb_refused"):
                    try:
                        edited = edited_fields(client.ci_lifecycle(kept.sys_id), want)
                    except ServiceNowError as exc:
                        # a refused record's CI that can't be read (deleted in ServiceNow: a 404) is reconciled again, which creates it
                        if kept.last_error is None or not _ci_refusal(exc):
                            raise
                        edited = []
                    if not edited and kept.last_error is None:
                        counts["unchanged"] += 1
                        continue
                done = client.reconcile(ci_values(row, owner, github_web, state))  # an orphan's without owned_by
                if done.errors:
                    store.record_cmdb_error(repo=row.repo, error="cmdb_refused", now=now)
                    counts["refused"] += 1
                    continue
                number = client.number(done.sys_id)
                # an edit written back is read again: the instance may answer and keep the edit (a rule or an ACL on it), and then no
                # note may say the portal's state was written back
                if edited and edited_fields(client.ci_lifecycle(done.sys_id), want):
                    store.record_cmdb_error(repo=row.repo, error="cmdb_refused", now=now)
                    counts["refused"] += 1
                    continue
                if orphan and kept.fields_hash != digest:
                    edited = []  # the portal's own change (its state, or the owner gone): a person changed nothing in ServiceNow
            except ServiceNowError as exc:
                if _ci_refusal(exc):
                    # ServiceNow refused this CI (a CI deleted there is a 404, an ACL on it a 403): this row's error, and the next pass
                    # reconciles it again; the rest still go
                    store.record_cmdb_error(repo=row.repo, error="cmdb_refused", now=now)
                    counts["refused"] += 1
                    continue
                store.record_cmdb_error(repo=row.repo, error="cmdb_unreachable", now=now)
                counts["unreachable"] += 1
                break  # the rest wait for the next pass
            if edited and state is not None:  # the CI's latest lifecycle task: the one of the action that set the state
                # the note before the sync: a pass that stops between the two notes it again on the next pass (at least once), and a
                # sync is never recorded without its note; a note the store refuses is this row's error, and the pass goes on
                task = f" for {state.task}" if state.task else ""
                try:
                    store.add_lifecycle_event(request_id=state.request, step="ci_corrected", now=now, note=(
                        f"{row.repo}'s CI lifecycle fields were changed in ServiceNow ({', '.join(edited)}). The collector wrote back the "
                        f"portal's state, {want['u_golden_road_state']}{task}: the platform's state is what it applied in git (rule 9)."))
                except Exception:  # noqa: BLE001 - the store's refusal or failure, whatever its type: this row's error only
                    store.record_cmdb_error(repo=row.repo, error="cmdb_refused", now=now)
                    counts["refused"] += 1
                    continue
            store.record_cmdb_sync(repo=row.repo, sys_id=done.sys_id, number=number, fields_hash=digest, operation=done.operation, now=now)
            if orphan:  # sent without its owner: the hash says so, and the row's warning stays
                store.record_cmdb_error(repo=row.repo, error="cmdb_owner_unknown", now=now)
                counts["owner_unknown"] += 1
            else:
                counts["synced"] += 1
            if edited:
                counts["corrected"] += 1
    problem = ("cmdb_unreachable" if counts["unreachable"] else "cmdb_owner_unknown" if counts["owner_unknown"]
               else "cmdb_refused" if counts["refused"] else None)
    store.set_rereview_status(source="cmdb", problem=problem, counts=counts, now=now)
    return {**counts, "problem": problem}
