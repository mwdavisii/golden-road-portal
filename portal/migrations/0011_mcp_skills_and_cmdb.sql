-- 0011: the CMDB record of each intake record (builder 0.6.0, spec 2026-10-31 Part 3), the registration's ownership acknowledgement,
-- and the collector's cmdb part on the Re-review page. Forward-only. Run as portal_owner by python -m portal.migrate. (0001 to 0010
-- stay as they were applied.)

-- 1. The acknowledgement on the register form: NULL for every request before 0011 and for every binding. The web writes it with the
-- insert and never changes it (portal_web's UPDATE on request is by column since 0002, and this column is not one of them).
ALTER TABLE request ADD COLUMN ownership_acknowledged_at timestamptz;
ALTER TABLE request ADD CONSTRAINT request_ownership_acknowledged_registration
  CHECK (ownership_acknowledged_at IS NULL OR kind = 'registration');

-- 2. Each intake record as the collector last synced it to ServiceNow (a cache: the intake file in git is the truth). A row has the
-- CI (after a good sync) or an error (an owner unknown before any sync), or both (a later attempt failed; the CI is kept).
CREATE TABLE cmdb_record (
  repo text PRIMARY KEY CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  sys_id text CHECK (sys_id ~ '^[0-9a-f]{32}$'),
  number text CHECK (number ~ '^[A-Z]{2,8}[0-9]{4,12}$'),
  fields_hash text CHECK (fields_hash ~ '^[0-9a-f]{64}$'),  -- of the last good sync
  last_operation text CHECK (last_operation IN ('INSERT', 'UPDATE', 'NO_CHANGE')),
  last_synced_at timestamptz,
  last_error text CHECK (last_error IN ('cmdb_unreachable', 'cmdb_owner_unknown', 'cmdb_refused')),
  last_error_at timestamptz,
  CONSTRAINT cmdb_record_ci_whole CHECK ((sys_id IS NULL) = (number IS NULL) AND (sys_id IS NULL) = (fields_hash IS NULL)
                                         AND (sys_id IS NULL) = (last_operation IS NULL) AND (sys_id IS NULL) = (last_synced_at IS NULL)),
  CONSTRAINT cmdb_record_error_whole CHECK ((last_error IS NULL) = (last_error_at IS NULL)),
  CONSTRAINT cmdb_record_ci_or_error CHECK (sys_id IS NOT NULL OR last_error IS NOT NULL)
);
CREATE UNIQUE INDEX cmdb_record_repo_any_case ON cmdb_record (lower(repo));

-- Times are the database's: a good sync sets last_synced_at and clears the error; an error sets last_error_at and keeps the CI.
CREATE FUNCTION cmdb_record_times() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.repo IS DISTINCT FROM OLD.repo THEN
    RAISE EXCEPTION 'a CMDB record''s repo never changes';
  END IF;
  IF NEW.last_error IS NULL THEN
    NEW.last_synced_at := now();
    NEW.last_error_at := NULL;
  ELSE
    NEW.last_error_at := now();
    IF TG_OP = 'UPDATE' THEN
      NEW.sys_id := OLD.sys_id; NEW.number := OLD.number; NEW.fields_hash := OLD.fields_hash;
      NEW.last_operation := OLD.last_operation; NEW.last_synced_at := OLD.last_synced_at;
    END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER cmdb_record_write BEFORE INSERT OR UPDATE ON cmdb_record FOR EACH ROW EXECUTE FUNCTION cmdb_record_times();
-- The CI outlives the app here (retiring a CI is out of scope): nobody deletes a row, the owner included.
CREATE TRIGGER cmdb_record_no_delete BEFORE DELETE ON cmdb_record FOR EACH ROW EXECUTE FUNCTION rereview_is_insert_only();
CREATE TRIGGER cmdb_record_no_truncate BEFORE TRUNCATE ON cmdb_record FOR EACH STATEMENT EXECUTE FUNCTION rereview_is_insert_only();

-- 3. The collector's fifth part. 0010 declared both checks on the columns; Postgres named the source check after its column
-- (rereview_source_source_check) and the problem check, which reads both columns, after the table (rereview_source_check).
ALTER TABLE rereview_source DROP CONSTRAINT rereview_source_source_check;
ALTER TABLE rereview_source ADD CONSTRAINT rereview_source_source_check
  CHECK (source IN ('repo_admins', 'evidence', 'intake', 'events', 'cmdb'));
ALTER TABLE rereview_source DROP CONSTRAINT rereview_source_check;
ALTER TABLE rereview_source ADD CONSTRAINT rereview_source_problem_check
  CHECK (problem = 'failed' OR (source = 'intake' AND problem IN ('factory_unreadable'))
         OR (source = 'events' AND problem IN ('actions_unreadable', 'factory_unreadable', 'factory_not_main', 'artifact_host_refused',
                                               'github_busy'))
         OR (source = 'cmdb' AND problem IN ('cmdb_unreachable', 'cmdb_owner_unknown', 'cmdb_refused')));

-- 4. Who does what: the collector writes cmdb_record and reads a person's email to find their ServiceNow user; the web reads it.
GRANT SELECT, INSERT, UPDATE ON cmdb_record TO portal_collector;
GRANT SELECT ON cmdb_record TO portal_web;
GRANT SELECT (id, email) ON person TO portal_collector;

-- 5. The acknowledgement's time is the database's (final review of 0.6.0): 0007's insert guard, unchanged, plus a non-NULL
-- ownership_acknowledged_at set to now(), so no caller can backdate it. NULL stays NULL (a request without one).
CREATE OR REPLACE FUNCTION request_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL
     OR NEW.failure_reason IS NOT NULL OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL OR NEW.submission <> 1
     OR NEW.floor_override IS NOT NULL OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending (a binding: approved) with no decision or result';
  END IF;
  IF NEW.kind = 'binding' THEN
    IF NEW.state <> 'approved' OR NEW.final IS NULL OR NEW.final IS DISTINCT FROM NEW.draft
       OR NEW.final->>'schema' IS DISTINCT FROM 'binding/v2' OR NEW.final->>'request' IS DISTINCT FROM NEW.id
       OR NEW.final->>'linked_by' IS DISTINCT FROM NEW.requested_by OR NEW.final->>'repo' IS DISTINCT FROM NEW.repo
       OR NEW.final->>'environment' IS NULL OR NEW.final->>'environment' NOT IN ('test', 'prod') THEN
      RAISE EXCEPTION 'a binding request starts approved, its record is its draft, and it names its request, person, repo and environment';
    END IF;
  ELSIF NEW.state <> 'pending' OR NEW.final IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending with no decision or result';
  END IF;
  IF NEW.ownership_acknowledged_at IS NOT NULL THEN
    NEW.ownership_acknowledged_at := now();
  END IF;
  RETURN NEW;
END
$$;
