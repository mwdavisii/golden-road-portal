"""The collector's CMDB part (spec 2026-10-31 N1-N4): every intake record the factory holds is a business application in ServiceNow.
Each pass sends each readable record that is new, whose synced fields or intake file changed since the last good sync, or whose last
attempt failed, through the Identification and Reconciliation API (identified by correlation_id), and records the CI's sys_id and number
or the error.
The intake file in git stays the truth; ServiceNow follows it, never the other way (N3). An intake/v2 record whose owner matches no
single ServiceNow user is not sent (a CI without its owner is an orphaned app): it is cmdb_owner_unknown and retried every pass. When
ServiceNow can't be reached the pass stops there; the rest wait for the next pass, and the part's status says so."""

import hashlib
import json

from ..servicenow import ServiceNowError, email_ok

SHORT_MAX = 1000  # short_description: ServiceNow's field is shorter than a purpose (confirm against the instance)


def _short(purpose: str) -> str:
    return purpose if len(purpose) <= SHORT_MAX else purpose[:SHORT_MAX - 3] + "..."


def correlation_id(repo: str) -> str:
    """Lowercase: GitHub names are case-insensitive, so a rename that only changes case updates the same CI."""
    return f"github:{repo.lower()}"


def ci_values(row, owner_sys_id, github_web) -> dict:
    """N2's fields for one observed record. owned_by only for a record with an owner of record (intake/v2)."""
    values = {"name": row.repo, "short_description": _short(row.purpose), "u_tier": row.tier,
              "u_data_classification": row.data_classification, "u_repo_url": f"{github_web}/{row.repo}",
              "correlation_id": correlation_id(row.repo), "discovery_source": "golden_road"}
    if row.review_by is not None:
        values["u_review_by"] = row.review_by.isoformat()
    if owner_sys_id is not None:
        values["owned_by"] = owner_sys_id
    return values


def fields_hash(row, owner_email, owner_sys_id, github_web) -> str:
    """What decides a re-send: N2's inputs (the repo URL's host among them), the owner's email (a new email may match a ServiceNow user),
    the user it matches now (another user may take the email over, and owned_by must follow), and the intake file's blob: each new file
    (a renewal with the same values among them) is reconciled once, ServiceNow answers NO_CHANGE, and last_synced_at follows the merge."""
    doc = {"repo": row.repo, "blob_sha": row.blob_sha, "schema": row.schema,
           "purpose": row.purpose, "tier": row.tier, "classification": row.data_classification,
           "review_by": row.review_by.isoformat() if row.review_by else None, "owner_email": (owner_email or "").lower(),
           "owner_sys_id": owner_sys_id, "github_web": github_web}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seed_mock(servicenow, store) -> None:
    """At collector start: the mock's CI table from the CIs the portal recorded, so a restart keeps their sys_ids and numbers. The
    portal keeps no copy of the values it sent, so each is seeded without them."""
    servicenow.seed([(correlation_id(r.repo), r.sys_id, r.number, None) for r in store.cmdb_records() if r.sys_id is not None])


def collect_cmdb(store, servicenow, *, github_web, now) -> dict:
    """One pass. Returns the counts (synced, unchanged, owner_unknown, refused, unreachable) and the part's problem, which it also
    records as the cmdb part's status."""
    rows = [r for r in store.intake_observed() if r.problem is None]
    records = {r.repo.lower(): r for r in store.cmdb_records()}
    counts = {"synced": 0, "unchanged": 0, "owner_unknown": 0, "refused": 0, "unreachable": 0}
    with servicenow.client() as client:
        for row in rows:
            email = store.person_email(row.owner) if row.schema == "intake/v2" else None
            try:
                # an owner of record's user is looked up every pass: the hash holds who it is now
                owner = client.user_sys_id(email) if row.schema == "intake/v2" and email_ok(email) else None
                digest = fields_hash(row, email, owner, github_web)
                kept = records.get(row.repo.lower())
                if kept is not None and kept.fields_hash == digest and kept.last_error is None:
                    counts["unchanged"] += 1
                    continue
                if row.schema == "intake/v2" and owner is None:
                    store.record_cmdb_error(repo=row.repo, error="cmdb_owner_unknown", now=now)
                    counts["owner_unknown"] += 1
                    continue
                done = client.reconcile(ci_values(row, owner, github_web))
                if done.errors:
                    store.record_cmdb_error(repo=row.repo, error="cmdb_refused", now=now)
                    counts["refused"] += 1
                    continue
                number = client.number(done.sys_id)
            except ServiceNowError:
                store.record_cmdb_error(repo=row.repo, error="cmdb_unreachable", now=now)
                counts["unreachable"] += 1
                break  # the rest wait for the next pass
            store.record_cmdb_sync(repo=row.repo, sys_id=done.sys_id, number=number, fields_hash=digest, operation=done.operation, now=now)
            counts["synced"] += 1
    problem = ("cmdb_unreachable" if counts["unreachable"] else "cmdb_owner_unknown" if counts["owner_unknown"]
               else "cmdb_refused" if counts["refused"] else None)
    store.set_rereview_status(source="cmdb", problem=problem, counts=counts, now=now)
    return {**counts, "problem": problem}
