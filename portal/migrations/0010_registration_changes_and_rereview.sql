-- 0010: registration changes and the re-review list (builder 0.5.1, spec 2026-10-31). A change's evidence carries the record it
-- replaces (its blob SHA and what it says), and its approval keeps that record's scorecard mark. The collector caches the factory's
-- intake records and the scorecard's re-review events, records each run it looked at, and opens re-review items; an admin closes an
-- item with a note, the platform closes a date item whose date moved. Caches are written only by the collector (it replaces the intake
-- records whole and updates each source's status); items are closed once and never reopened; the runs, the events, the items and
-- their events are never otherwise updated or deleted. Times are the database's. (0001 to 0009 stay as they were applied.)

-- 1. The record a change replaces, on its evidence (registration_evidence stays insert-only; rows before 0010 have neither value).
ALTER TABLE registration_evidence ADD COLUMN record_sha text CHECK (record_sha ~ '^[0-9a-f]{40}$');
ALTER TABLE registration_evidence ADD COLUMN record jsonb;
ALTER TABLE registration_evidence ADD CONSTRAINT registration_evidence_record_whole CHECK (
  (record_sha IS NULL) = (record IS NULL) AND (problem IS NULL OR record_sha IS NULL)
  AND (record IS NULL OR (jsonb_typeof(record) = 'object' AND record->>'schema' IN ('intake/v1', 'intake/v2', 'invalid'))));
ALTER TABLE registration_evidence DROP CONSTRAINT registration_evidence_problem_check;
ALTER TABLE registration_evidence ADD CONSTRAINT registration_evidence_problem_check
  CHECK (problem IN ('rules_missing', 'rules_invalid', 'rules_unreadable', 'repo_unreadable', 'record_unreadable', 'record_case_mismatch'));

-- An approval keeps the scorecard mark of the record a change replaces (a first registration's evidence has no record, so no mark):
-- 0008's guard with that one rule added, as check_approval has it.
CREATE OR REPLACE FUNCTION request_approval_floor_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  ev record;
  tiers text[] := ARRAY['low', 'moderate', 'high'];
  classes text[] := ARRAY['public', 'confidential', 'protected'];
  below boolean;
BEGIN
  IF NOT (OLD.state = 'pending' AND NEW.state = 'approved') THEN RETURN NEW; END IF;
  SELECT commit_sha, floor_tier, floor_classification, record INTO ev FROM registration_evidence
   WHERE request_id = NEW.id AND submission = NEW.submission AND problem IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'an approval needs the platform''s evidence for this submission'; END IF;
  IF array_position(classes, ev.floor_classification) IS NULL THEN
    RAISE EXCEPTION 'the evidence names a retired classification level: send the request back so the platform computes it again';
  END IF;
  IF NEW.final->>'evidence_commit' IS DISTINCT FROM ev.commit_sha OR NEW.final->>'floor_tier' IS DISTINCT FROM ev.floor_tier
     OR NEW.final->>'floor_classification' IS DISTINCT FROM ev.floor_classification THEN
    RAISE EXCEPTION 'the approved record carries the stored evidence commit and floor';
  END IF;
  IF NEW.final->>'scorecard' IS DISTINCT FROM ev.record->>'scorecard' THEN
    RAISE EXCEPTION 'the approved record keeps the scorecard mark of the record it replaces';
  END IF;
  IF NEW.final->'floor_override' IS DISTINCT FROM NEW.floor_override THEN
    RAISE EXCEPTION 'the record and the request carry the same override';
  END IF;
  below := coalesce(array_position(tiers, NEW.final->>'tier'), 0) < array_position(tiers, ev.floor_tier)
        OR coalesce(array_position(classes, NEW.final->>'data_classification'), 0) < array_position(classes, ev.floor_classification);
  IF below THEN
    IF NEW.floor_override IS NULL OR NEW.floor_override->>'by' IS DISTINCT FROM NEW.decided_by
       OR length(coalesce(NEW.floor_override->>'reason', '')) < 30 THEN
      RAISE EXCEPTION 'an approval below the floor needs the approver''s override reason of at least 30 characters';
    END IF;
    IF NEW.floor_override->>'at' IS DISTINCT FROM NEW.final->>'approved_at' OR length(NEW.floor_override->>'reason') > 1000
       OR NEW.floor_override->>'reason' ~ '[\r\n]' THEN
      RAISE EXCEPTION 'the override carries the approval time and a single line of at most 1000 characters';
    END IF;
  ELSIF NEW.floor_override IS NOT NULL THEN
    RAISE EXCEPTION 'an override is recorded only for an approval below the floor';
  END IF;
  RETURN NEW;
END
$$;

-- 2. The factory's intake records as the collector last read them (replaced whole each pass).
CREATE TABLE intake_observed (
  repo text PRIMARY KEY CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  blob_sha text NOT NULL CHECK (blob_sha ~ '^[0-9a-f]{40}$'),
  schema text CHECK (schema IN ('intake/v1', 'intake/v2')),
  by_portal boolean NOT NULL,
  owner text,
  tier text CHECK (tier IN ('low', 'moderate', 'high')),
  data_classification text CHECK (data_classification IN ('public', 'confidential', 'protected')),
  purpose text CHECK (length(purpose) <= 4000),
  review_by date,
  scorecard text CHECK (scorecard IN ('per-app', 'central')),
  problem text CHECK (problem IN ('invalid', 'unreadable', 'not_a_file', 'case_mismatch')),
  observed_at timestamptz NOT NULL,
  CHECK ((problem IS NULL) = (schema IS NOT NULL)),
  CHECK (problem IS NOT NULL OR (owner IS NOT NULL AND tier IS NOT NULL AND data_classification IS NOT NULL AND purpose IS NOT NULL)),
  CHECK (schema IS DISTINCT FROM 'intake/v2' OR review_by IS NOT NULL),
  CHECK (schema IS DISTINCT FROM 'intake/v1' OR review_by IS NULL)
);
CREATE UNIQUE INDEX intake_observed_repo_any_case ON intake_observed (lower(repo));

-- 3. Each scorecard run the collector looked at, once, and what it found (so an artifact is downloaded once, and a bad one is counted).
CREATE TABLE rereview_run (
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  outcome text NOT NULL CHECK (outcome IN ('event', 'duplicate', 'invalid', 'no_event', 'no_artifact', 'expired', 'too_large',
                                           'not_registered', 'repo_mismatch', 'unreadable', 'untrusted_run')),
  seen_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX rereview_run_once ON rereview_run (lower(repo), run_id);

-- 4. The re-review events (re-review-event/v1, validated before insert), one per source, repo and head: a per-app event never hides
-- the central one of the same head.
CREATE TABLE rereview_event (
  source text NOT NULL DEFAULT 'central' CHECK (source IN ('central', 'per-app')),
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  head_sha text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  pr integer CHECK (pr BETWEEN 1 AND 999999999),
  level text NOT NULL CHECK (level IN ('standard', 'elevated', 'security_review')),
  floor text NOT NULL CHECK (floor IN ('standard', 'elevated', 'security_review')),
  complete boolean NOT NULL,
  created timestamptz NOT NULL,
  tree_sha text NOT NULL CHECK (tree_sha ~ '^[0-9a-f]{40}$'),
  scorecard_version text NOT NULL CHECK (scorecard_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  signals jsonb NOT NULL CHECK (jsonb_typeof(signals) = 'array' AND jsonb_array_length(signals) <= 40),
  run_repo text NOT NULL CHECK (run_repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  observed_at timestamptz NOT NULL,
  PRIMARY KEY (source, repo, head_sha)
);
CREATE UNIQUE INDEX rereview_event_once ON rereview_event (source, lower(repo), head_sha);

-- 5. The re-review items (authoritative).
CREATE TABLE rereview_item (
  id text PRIMARY KEY CHECK (id ~ '^i_[0-9a-f]{32}$'),
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  reason text NOT NULL CHECK (reason IN ('review_overdue', 'review_due_soon', 'scorecard_signal')),
  key text NOT NULL CHECK ((reason = 'scorecard_signal' AND key ~ '^[0-9a-f]{40}$') OR (reason <> 'scorecard_signal' AND key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')),
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  close_kind text CHECK (close_kind IN ('person', 'auto')),
  closed_by text REFERENCES person (id),  -- as with request.decided_by, the database can't tie it to the signed-in person: the web does
  close_note text,
  CHECK ((closed_at IS NULL) = (close_kind IS NULL) AND (closed_at IS NULL) = (close_note IS NULL)),
  CHECK (close_note IS NULL OR (length(close_note) BETWEEN 10 AND 1000 AND close_note !~ '[\r\n]')),
  CHECK (close_kind IS DISTINCT FROM 'person' OR closed_by IS NOT NULL),
  CHECK (close_kind IS DISTINCT FROM 'auto' OR (closed_by IS NULL AND reason <> 'scorecard_signal'))
);
CREATE UNIQUE INDEX rereview_item_once ON rereview_item (lower(repo), reason, key);
CREATE UNIQUE INDEX rereview_item_one_open ON rereview_item (lower(repo), reason) WHERE closed_at IS NULL;

CREATE TABLE rereview_item_event (
  item_id text NOT NULL REFERENCES rereview_item (id),
  source text NOT NULL DEFAULT 'central',
  repo text NOT NULL,
  head_sha text NOT NULL,
  matched jsonb NOT NULL CHECK (jsonb_typeof(matched) = 'array' AND jsonb_array_length(matched) BETWEEN 1 AND 40),
  added_at timestamptz NOT NULL,
  PRIMARY KEY (item_id, source, head_sha),
  FOREIGN KEY (source, repo, head_sha) REFERENCES rereview_event (source, repo, head_sha)
);

-- 6. Each run source's watermark: every run above it is done (recorded, or not a success), so a walk stops there.
CREATE TABLE rereview_watermark (
  repo text PRIMARY KEY CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX rereview_watermark_repo_any_case ON rereview_watermark (lower(repo));

-- 7. Completed runs that didn't succeed, re-checked each pass until a later attempt succeeds or their artifacts are gone (a cache).
CREATE TABLE rereview_failed_run (
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  attempt integer NOT NULL CHECK (attempt >= 1),
  last_attempt_at timestamptz NOT NULL,
  retention_days integer NOT NULL CHECK (retention_days BETWEEN 1 AND 400),
  seen_at timestamptz NOT NULL,
  checked_at timestamptz NOT NULL  -- after a day, a row is checked at most hourly
);
CREATE UNIQUE INDEX rereview_failed_run_once ON rereview_failed_run (lower(repo), run_id);

-- Passes GitHub couldn't answer for one run: after 12 it is recorded unreadable, so one run can't stop older ones being read.
CREATE TABLE rereview_stuck_run (
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  passes integer NOT NULL CHECK (passes >= 1),
  first_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX rereview_stuck_run_once ON rereview_stuck_run (lower(repo), run_id);

-- 8. Each source's last problem and counts.
CREATE TABLE rereview_source (
  source text PRIMARY KEY CHECK (source IN ('repo_admins', 'evidence', 'intake', 'events')),  -- each collector part
  problem text CHECK (problem = 'failed' OR (source = 'intake' AND problem IN ('factory_unreadable'))
                      OR (source = 'events' AND problem IN ('actions_unreadable', 'factory_unreadable', 'factory_not_main',
                                                            'artifact_host_refused', 'github_busy'))),
  counts jsonb NOT NULL CHECK (jsonb_typeof(counts) = 'object'),
  checked_at timestamptz NOT NULL,
  last_ok_at timestamptz  -- the last time the part finished with no problem (set by the database)
);

-- A trusted run's valid event whose app has no readable intake record yet: kept and read again each pass (it holds nothing back).
CREATE TABLE rereview_pending_event (
  source text NOT NULL CHECK (source IN ('central', 'per-app')),
  run_repo text NOT NULL CHECK (run_repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  run_id bigint NOT NULL CHECK (run_id > 0),
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  doc jsonb NOT NULL CHECK (jsonb_typeof(doc) = 'object'),
  seen_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX rereview_pending_event_once ON rereview_pending_event (lower(run_repo), run_id);

-- The rules.
CREATE FUNCTION rereview_is_insert_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is insert-only', TG_TABLE_NAME;
END
$$;
CREATE TRIGGER rereview_run_no_change BEFORE UPDATE OR DELETE ON rereview_run FOR EACH ROW EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_run_no_truncate BEFORE TRUNCATE ON rereview_run FOR EACH STATEMENT EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_event_no_change BEFORE UPDATE OR DELETE ON rereview_event FOR EACH ROW EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_event_no_truncate BEFORE TRUNCATE ON rereview_event FOR EACH STATEMENT EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_item_event_no_change BEFORE UPDATE OR DELETE ON rereview_item_event FOR EACH ROW EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_item_event_no_truncate BEFORE TRUNCATE ON rereview_item_event FOR EACH STATEMENT EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_item_no_delete BEFORE DELETE ON rereview_item FOR EACH ROW EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER rereview_item_no_truncate BEFORE TRUNCATE ON rereview_item FOR EACH STATEMENT EXECUTE FUNCTION rereview_is_insert_only();

-- Every time here is the database's, whatever an INSERT (or the status upsert's UPDATE) says. rereview_item_event's is set by its guard.
CREATE FUNCTION rereview_times() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_TABLE_NAME = 'rereview_item' THEN
    IF NEW.closed_at IS NOT NULL OR NEW.close_kind IS NOT NULL OR NEW.closed_by IS NOT NULL OR NEW.close_note IS NOT NULL THEN
      RAISE EXCEPTION 'an item opens open';
    END IF;
    NEW.opened_at := now();
  ELSIF TG_TABLE_NAME IN ('rereview_event', 'intake_observed') THEN
    NEW.observed_at := now();
  ELSIF TG_TABLE_NAME = 'rereview_failed_run' THEN
    NEW.seen_at := CASE WHEN TG_OP = 'UPDATE' THEN OLD.seen_at ELSE now() END;
    NEW.checked_at := now();
  ELSIF TG_TABLE_NAME = 'rereview_stuck_run' THEN
    NEW.first_at := CASE WHEN TG_OP = 'UPDATE' THEN OLD.first_at ELSE now() END;
  ELSIF TG_TABLE_NAME IN ('rereview_run', 'rereview_pending_event') THEN
    NEW.seen_at := now();
  ELSIF TG_TABLE_NAME = 'rereview_source' THEN
    NEW.checked_at := now();
    IF NEW.problem IS NULL THEN
      NEW.last_ok_at := now();
    ELSIF TG_OP = 'UPDATE' THEN
      NEW.last_ok_at := OLD.last_ok_at;
    ELSE
      NEW.last_ok_at := NULL;
    END IF;
  ELSIF TG_TABLE_NAME = 'rereview_watermark' THEN
    IF TG_OP = 'UPDATE' AND NEW.run_id < OLD.run_id THEN
      RAISE EXCEPTION 'a watermark never falls';
    END IF;
    NEW.updated_at := now();
  ELSE
    RAISE EXCEPTION 'rereview_times() does not know %', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER rereview_item_insert BEFORE INSERT ON rereview_item FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_event_insert BEFORE INSERT ON rereview_event FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER intake_observed_insert BEFORE INSERT ON intake_observed FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_run_insert BEFORE INSERT ON rereview_run FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_failed_run_insert BEFORE INSERT OR UPDATE ON rereview_failed_run FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_stuck_run_write BEFORE INSERT OR UPDATE ON rereview_stuck_run FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_pending_event_insert BEFORE INSERT ON rereview_pending_event FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_source_write BEFORE INSERT OR UPDATE ON rereview_source FOR EACH ROW EXECUTE FUNCTION rereview_times();
CREATE TRIGGER rereview_watermark_write BEFORE INSERT OR UPDATE ON rereview_watermark FOR EACH ROW EXECUTE FUNCTION rereview_times();

CREATE FUNCTION rereview_item_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE it record;
BEGIN
  -- FOR SHARE: a close in flight (its row lock) is waited for, and its result is what this reads
  SELECT repo, reason, closed_at INTO it FROM rereview_item WHERE id = NEW.item_id FOR SHARE;
  IF NOT FOUND OR it.closed_at IS NOT NULL OR it.reason <> 'scorecard_signal' OR lower(it.repo) <> lower(NEW.repo) THEN
    RAISE EXCEPTION 'an event joins only an open scorecard item of its own repo';
  END IF;
  NEW.added_at := now();
  RETURN NEW;
END
$$;
CREATE TRIGGER rereview_item_event_insert BEFORE INSERT ON rereview_item_event FOR EACH ROW EXECUTE FUNCTION rereview_item_event_guard();

CREATE FUNCTION rereview_item_close_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.repo IS DISTINCT FROM OLD.repo OR NEW.reason IS DISTINCT FROM OLD.reason
     OR NEW.key IS DISTINCT FROM OLD.key OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
    RAISE EXCEPTION 'an item''s repo, reason, key and opening never change';
  END IF;
  IF OLD.closed_at IS NOT NULL OR NEW.closed_at IS NULL THEN
    RAISE EXCEPTION 'an item is closed once and never reopened';
  END IF;
  IF current_user = 'portal_web' AND NEW.close_kind IS DISTINCT FROM 'person' THEN
    RAISE EXCEPTION 'the web closes an item as a person';
  END IF;
  IF current_user = 'portal_collector' AND NEW.close_kind IS DISTINCT FROM 'auto' THEN
    RAISE EXCEPTION 'the collector closes only a date item, as the platform';
  END IF;
  IF NEW.close_kind = 'person' AND EXISTS (SELECT 1 FROM intake_observed o WHERE lower(o.repo) = lower(NEW.repo) AND o.problem IS NOT NULL) THEN
    RAISE EXCEPTION 'the app''s intake record can''t be read, so who owns it is not known';
  END IF;
  -- the owner is the record's, and the requester of the app's last approved registration (the record may not show it yet)
  IF NEW.close_kind = 'person' AND (EXISTS (SELECT 1 FROM intake_observed o WHERE lower(o.repo) = lower(NEW.repo) AND o.owner = NEW.closed_by)
     OR (SELECT r.requested_by FROM request r WHERE r.kind = 'registration' AND lower(r.repo) = lower(NEW.repo)
         AND r.state IN ('approved', 'writing', 'pr_open', 'merged') ORDER BY r.created_at DESC, r.id DESC LIMIT 1) = NEW.closed_by) THEN
    RAISE EXCEPTION 'an admin who owns the app can''t close its item';
  END IF;
  NEW.closed_at := now();
  RETURN NEW;
END
$$;
CREATE TRIGGER rereview_item_close BEFORE UPDATE ON rereview_item FOR EACH ROW EXECUTE FUNCTION rereview_item_close_guard();

-- The collector audits what it did to an item, as the system, and nothing else (it still can't read the log): the item exists; an
-- added event is on it; a close is the platform's own; and each of these is audited once. The collector can't read audit_log, so
-- "once" is asked of a function that runs as its owner and answers only yes or no.
CREATE FUNCTION rereview_audit_exists(subject text, act text, head text, src text) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
  SET search_path = public, pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM audit_log WHERE subject_kind = 'rereview_item' AND subject_id = subject AND action = act
                 AND (head IS NULL OR (after->>'head_sha' = head AND coalesce(after->>'source', 'central') = src)))
$$;
REVOKE ALL ON FUNCTION rereview_audit_exists(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rereview_audit_exists(text, text, text, text) TO portal_collector;

CREATE FUNCTION audit_log_collector_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_user = 'portal_collector' AND (NEW.actor_kind <> 'system' OR NEW.actor_id IS NOT NULL OR NEW.subject_kind <> 'rereview_item'
     OR NEW.action NOT IN ('rereview_item.opened', 'rereview_item.event_added', 'rereview_item.closed')
     OR NOT EXISTS (SELECT 1 FROM rereview_item WHERE id = NEW.subject_id)
     OR (NEW.action = 'rereview_item.event_added' AND NOT EXISTS (SELECT 1 FROM rereview_item_event
                                                                 WHERE item_id = NEW.subject_id AND head_sha = NEW.after->>'head_sha'
                                                                 AND source = coalesce(NEW.after->>'source', 'central')))
     OR (NEW.action = 'rereview_item.closed' AND NOT EXISTS (SELECT 1 FROM rereview_item WHERE id = NEW.subject_id AND close_kind = 'auto'))
     OR rereview_audit_exists(NEW.subject_id, NEW.action, CASE WHEN NEW.action = 'rereview_item.event_added' THEN NEW.after->>'head_sha' END,
                              coalesce(NEW.after->>'source', 'central'))) THEN
    RAISE EXCEPTION 'the collector writes only system entries about re-review items';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER audit_log_collector BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_collector_guard();

GRANT SELECT, INSERT, DELETE ON intake_observed TO portal_collector;
GRANT SELECT, INSERT, UPDATE, DELETE ON rereview_failed_run TO portal_collector;
GRANT SELECT, INSERT, DELETE ON rereview_pending_event TO portal_collector;
GRANT SELECT, INSERT, UPDATE, DELETE ON rereview_stuck_run TO portal_collector;
GRANT SELECT, INSERT ON rereview_run, rereview_event, rereview_item, rereview_item_event TO portal_collector;
GRANT SELECT, INSERT, UPDATE ON rereview_source, rereview_watermark TO portal_collector;
GRANT UPDATE (closed_at, close_kind, closed_by, close_note) ON rereview_item TO portal_collector, portal_web;
GRANT SELECT ON intake_observed, rereview_run, rereview_event, rereview_item, rereview_item_event, rereview_source, rereview_watermark,
  rereview_failed_run, rereview_pending_event TO portal_web;
GRANT INSERT ON audit_log TO portal_collector;
