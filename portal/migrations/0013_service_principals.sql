-- 0013: create a service principal (builder 0.7.2, spec 2026-11-16). A request of kind service_principal starts approved (pre-approved
-- scopes only) or pending (a scope that needs approval: a ServiceNow task decides it). Every later move is the collector's: it records
-- the task, reads its close, creates the principal (a service_principal row and the request created, in one transaction) and notes the
-- task. The mock ServiceNow's Approve and Reject are mock_task_decision rows the web inserts and both mocks replay. The kind has its
-- own insert and update triggers (0002's, 0005's and 0012's guards skip it). Forward-only. Run as portal_owner by python -m portal.migrate.

-- 1. The kind, its two final states, and its six columns (only this kind has them; only the collector sets them).
ALTER TABLE request DROP CONSTRAINT request_kind_check;
ALTER TABLE request ADD CONSTRAINT request_kind_check CHECK (kind IN ('registration', 'binding', 'lifecycle', 'service_principal'));
ALTER TABLE request DROP CONSTRAINT request_state_check;
ALTER TABLE request ADD CONSTRAINT request_state_check
  CHECK (state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open', 'merged', 'failed', 'created', 'rejected'));
ALTER TABLE request
  ADD COLUMN task_number text CHECK (task_number ~ '^SCTASK[0-9]{7}$'),
  ADD COLUMN task_sys_id text CHECK (task_sys_id ~ '^[0-9a-f]{32}$'),
  ADD COLUMN attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  ADD COLUMN last_error text CHECK (length(last_error) BETWEEN 1 AND 300),
  ADD COLUMN close_notes text CHECK (length(close_notes) <= 2000),
  ADD COLUMN note_sent_at timestamptz;
ALTER TABLE request ADD CONSTRAINT request_service_principal_columns CHECK (kind = 'service_principal' OR (task_number IS NULL
  AND task_sys_id IS NULL AND attempts = 0 AND last_error IS NULL AND close_notes IS NULL AND note_sent_at IS NULL));
ALTER TABLE request ADD CONSTRAINT request_task_pair CHECK ((task_number IS NULL) = (task_sys_id IS NULL));
ALTER TABLE request ADD CONSTRAINT request_created_or_rejected CHECK (state NOT IN ('created', 'rejected') OR kind = 'service_principal');
CREATE UNIQUE INDEX request_task_number ON request (task_number) WHERE task_number IS NOT NULL;

-- 2. One open request per kind and repo (a binding: per environment), as 0007; a service principal request is keyed by its own id (an
-- app may have several open), and no two open ones share a name (any case).
DROP INDEX request_one_open;
CREATE UNIQUE INDEX request_one_open ON request (kind, lower(repo), (CASE WHEN kind = 'binding' THEN coalesce(draft->>'environment', 'test')
  WHEN kind = 'service_principal' THEN id ELSE '' END))
  WHERE state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open');
CREATE UNIQUE INDEX request_service_principal_name ON request (lower(draft->>'display_name'))
  WHERE kind = 'service_principal' AND state IN ('pending', 'approved');

-- 3. The principals the collector created (S7, S8). The web and the writer read them (the directory overlay); the collector inserts
-- them; nobody changes or deletes one.
CREATE TABLE service_principal (
  id text PRIMARY KEY CHECK (id ~ '^sp-[0-9a-f]{24}$'),
  repo text NOT NULL CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  display_name text NOT NULL CHECK (display_name ~ '^sp-[a-z0-9-]{1,64}$'),
  client_id text NOT NULL UNIQUE CHECK (client_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'),
  scopes text[] NOT NULL CHECK (cardinality(scopes) >= 1),
  request_id text NOT NULL UNIQUE REFERENCES request (id),
  created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX service_principal_name ON service_principal (lower(display_name));

-- The row is the one its request asked for. SECURITY DEFINER: the collector may not read request.draft.
CREATE FUNCTION service_principal_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  req record;
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'a service principal is never changed or deleted'; END IF;
  -- the name's transaction lock, as the request insert guard takes it: a request of this name inserted meanwhile waits until this
  -- creation commits, then sees the principal (without it, it went in once the creating request left the open states, open for good)
  -- Two keys, 0013's namespace 1735552621 (0x67726E6D, "grnm") and the name's hash: a two-key advisory lock never meets a one-key
  -- one, so no name (whose hash an app admin could search for) can take the collector's session locks (SP_LOCK, WRITEBACK_LOCK).
  PERFORM pg_advisory_xact_lock(1735552621, hashtext(lower(NEW.display_name)));
  SELECT kind, repo, state, draft INTO req FROM request WHERE id = NEW.request_id;
  IF NOT FOUND OR req.kind IS DISTINCT FROM 'service_principal' THEN
    RAISE EXCEPTION 'a service principal names its service principal request';
  END IF;
  IF req.state <> 'approved' THEN RAISE EXCEPTION 'a service principal is created only for an approved request'; END IF;
  -- an app decommissioned after its request gets no principal (final review I1), as no request is taken for it (section 6)
  IF EXISTS (SELECT 1 FROM lifecycle_state WHERE lower(repo) = lower(req.repo) AND state = 'decommissioned') THEN
    RAISE EXCEPTION 'the app is decommissioned: no service principal is created for it';
  END IF;
  IF NEW.repo IS DISTINCT FROM req.repo OR NEW.display_name IS DISTINCT FROM req.draft->>'display_name'
     OR NEW.scopes IS DISTINCT FROM ARRAY(SELECT e.s FROM jsonb_array_elements_text(req.draft->'scopes') WITH ORDINALITY AS e(s, n) ORDER BY e.n) THEN
    RAISE EXCEPTION 'a service principal is the one its request asked for: its repo, name and scopes';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION service_principal_guard() FROM PUBLIC;
CREATE TRIGGER service_principal_write BEFORE INSERT OR UPDATE OR DELETE ON service_principal FOR EACH ROW
  EXECUTE FUNCTION service_principal_guard();
CREATE TRIGGER service_principal_no_truncate BEFORE TRUNCATE ON service_principal FOR EACH STATEMENT
  EXECUTE FUNCTION lifecycle_never_truncated();

-- 4. The mock approver's decisions (S6): the web inserts one per task, never the requester's own, and nobody changes one.
CREATE TABLE mock_task_decision (
  task_number text PRIMARY KEY CHECK (task_number ~ '^SCTASK[0-9]{7}$'),
  state text NOT NULL CHECK (state IN ('3', '4', '7')),
  close_notes text NOT NULL CHECK (length(close_notes) <= 2000),
  decided_by text NOT NULL REFERENCES person (id),
  decided_at timestamptz NOT NULL,
  CHECK (state = '3' OR length(btrim(close_notes)) >= 1)
);

-- The database checks only that the decider isn't the requester and that the task is an open service principal task's. That the
-- decider is a portal admin is the web's check (the mock's Approve and Reject are admin-only routes): mock only, no real approval.
CREATE FUNCTION mock_task_decision_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  req record;
BEGIN
  IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'a mock task decision is decided once and never changed'; END IF;
  SELECT state, requested_by INTO req FROM request WHERE task_number = NEW.task_number AND kind = 'service_principal';
  IF NOT FOUND OR req.state <> 'pending' THEN RAISE EXCEPTION 'only an open service principal task is decided in the mock'; END IF;
  IF req.requested_by = NEW.decided_by THEN RAISE EXCEPTION 'a requester can''t decide their own task'; END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION mock_task_decision_guard() FROM PUBLIC;
CREATE TRIGGER mock_task_decision_write BEFORE INSERT OR UPDATE OR DELETE ON mock_task_decision FOR EACH ROW
  EXECUTE FUNCTION mock_task_decision_guard();
CREATE TRIGGER mock_task_decision_no_truncate BEFORE TRUNCATE ON mock_task_decision FOR EACH STATEMENT
  EXECUTE FUNCTION lifecycle_never_truncated();

-- 5. What the collector reads of the kind: these rows and these columns only (never a registration's draft, 0004). A view runs as its
-- owner, portal_owner. security_barrier: the view's filter runs before any condition of the caller's, so a function the collector
-- writes (a cheap pg_temp one) never sees another kind's row.
CREATE VIEW service_principal_request WITH (security_barrier) AS
  SELECT id, repo, state, draft, requested_by, created_at, updated_at, task_number, task_sys_id, attempts, last_error, close_notes,
         note_sent_at
    FROM request WHERE kind = 'service_principal';

-- 6. The kind's own insert check; 0002's request_insert trigger (0012's guard function) now skips the kind.
CREATE FUNCTION request_insert_service_principal_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  keys text[];
  scopes text[];
BEGIN
  IF NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL
     OR NEW.failure_reason IS NOT NULL OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL OR NEW.submission <> 1
     OR NEW.floor_override IS NOT NULL OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL OR NEW.final IS NOT NULL OR NEW.ownership_acknowledged_at IS NOT NULL
     OR NEW.task_number IS NOT NULL OR NEW.task_sys_id IS NOT NULL OR NEW.attempts <> 0 OR NEW.last_error IS NOT NULL
     OR NEW.close_notes IS NOT NULL OR NEW.note_sent_at IS NOT NULL THEN
    RAISE EXCEPTION 'a service principal request starts with no decision, task or result';
  END IF;
  IF EXISTS (SELECT 1 FROM lifecycle_state WHERE lower(repo) = lower(NEW.repo) AND state = 'decommissioned') THEN
    RAISE EXCEPTION 'the app is decommissioned: no request is taken for it';
  END IF;
  IF jsonb_typeof(NEW.draft) IS DISTINCT FROM 'object' OR jsonb_typeof(NEW.draft->'scopes') IS DISTINCT FROM 'array'
     OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.draft->'scopes') AS e WHERE jsonb_typeof(e) <> 'string') THEN
    RAISE EXCEPTION 'a service principal request''s draft is a service-principal-request/v1';
  END IF;
  keys := (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(NEW.draft) AS k);
  scopes := ARRAY(SELECT e.s FROM jsonb_array_elements_text(NEW.draft->'scopes') WITH ORDINALITY AS e(s, n) ORDER BY e.n);
  IF NEW.state NOT IN ('approved', 'pending')
     OR keys IS DISTINCT FROM (CASE WHEN NEW.state = 'pending' THEN ARRAY['display_name', 'justification', 'repo', 'schema', 'scopes']
                                    ELSE ARRAY['display_name', 'repo', 'schema', 'scopes'] END)
     OR NEW.draft->>'schema' IS DISTINCT FROM 'service-principal-request/v1' OR NEW.draft->>'repo' IS DISTINCT FROM NEW.repo
     OR coalesce(NEW.draft->>'display_name', '') !~ '^sp-[a-z0-9-]{1,64}$'
     OR cardinality(scopes) = 0
     OR EXISTS (SELECT 1 FROM unnest(scopes) AS s WHERE s !~ '^[A-Za-z][A-Za-z]{0,63}(\.[A-Z][A-Za-z]{0,63}){1,3}$')
     OR scopes IS DISTINCT FROM ARRAY(SELECT d.s FROM (SELECT DISTINCT s FROM unnest(scopes) AS s) AS d ORDER BY d.s COLLATE "C")
     OR (NEW.state = 'pending' AND (jsonb_typeof(NEW.draft->'justification') IS DISTINCT FROM 'string'
         OR length(NEW.draft->>'justification') NOT BETWEEN 1 AND 2000
         OR NEW.draft->>'justification' <> btrim(NEW.draft->>'justification', E' \t\r\n'))) THEN
    RAISE EXCEPTION 'a service principal request starts approved or pending, its draft a service-principal-request/v1 for its repo: a name, sorted scopes, and a justification exactly when it is pending';
  END IF;
  -- the name's lock, as service_principal_guard takes it: a request of a name being created waits for that creation, then sees it
  PERFORM pg_advisory_xact_lock(1735552621, hashtext(lower(NEW.draft->>'display_name')));  -- two keys: see service_principal_guard
  IF EXISTS (SELECT 1 FROM service_principal WHERE lower(display_name) = lower(NEW.draft->>'display_name')) THEN
    RAISE EXCEPTION 'the name % is already used', NEW.draft->>'display_name';
  END IF;
  RETURN NEW;
END
$$;
DROP TRIGGER request_insert ON request;
CREATE TRIGGER request_insert BEFORE INSERT ON request FOR EACH ROW WHEN (NEW.kind <> 'service_principal')
  EXECUTE FUNCTION request_insert_guard();
CREATE TRIGGER request_insert_service_principal BEFORE INSERT ON request FOR EACH ROW WHEN (NEW.kind = 'service_principal')
  EXECUTE FUNCTION request_insert_service_principal_guard();

-- 7. The kind's moves, the collector's only: record the task once (pending), the task's close decides it (pending -> approved or
-- rejected, close notes only on a rejection), create it with its principal (approved -> created), note the task once (created), and
-- count a failure (one more attempt, with its error) in any of pending, approved and created. Nothing else of the request changes.
-- 0002's request_update trigger (0012's guard) and 0005's request_update_floor now skip the kind.
CREATE FUNCTION request_update_service_principal_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  pair text := OLD.state || '>' || NEW.state;
  moves text[] := ARRAY['pending>pending', 'pending>approved', 'pending>rejected', 'approved>approved', 'approved>created',
                        'created>created'];
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.repo IS DISTINCT FROM OLD.repo
     OR NEW.requested_by IS DISTINCT FROM OLD.requested_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'a request''s id, kind, repo, requester and creation time never change';
  END IF;
  IF current_user NOT IN ('portal_collector', 'portal_owner') THEN  -- an allow-list: a role added later is refused too
    RAISE EXCEPTION 'only the collector moves a service principal request';
  END IF;
  IF NOT pair = ANY (moves) THEN RAISE EXCEPTION 'illegal service principal request transition %', pair; END IF;
  IF NEW.draft IS DISTINCT FROM OLD.draft OR NEW.final IS DISTINCT FROM OLD.final OR NEW.submission IS DISTINCT FROM OLD.submission
     OR NEW.decided_by IS DISTINCT FROM OLD.decided_by OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
     OR NEW.decision_groups IS DISTINCT FROM OLD.decision_groups OR NEW.decision_note IS DISTINCT FROM OLD.decision_note
     OR NEW.floor_override IS DISTINCT FROM OLD.floor_override OR NEW.branch IS DISTINCT FROM OLD.branch
     OR NEW.pr_number IS DISTINCT FROM OLD.pr_number OR NEW.pr_url IS DISTINCT FROM OLD.pr_url OR NEW.merge_sha IS DISTINCT FROM OLD.merge_sha
     OR NEW.head_sha IS DISTINCT FROM OLD.head_sha OR NEW.base_sha IS DISTINCT FROM OLD.base_sha
     OR NEW.failure_reason IS DISTINCT FROM OLD.failure_reason
     OR NEW.ownership_acknowledged_at IS DISTINCT FROM OLD.ownership_acknowledged_at THEN
    RAISE EXCEPTION 'a service principal request changes only its state, its task, its attempts, its close notes and its note';
  END IF;
  IF OLD.task_number IS NOT NULL AND (NEW.task_number IS DISTINCT FROM OLD.task_number OR NEW.task_sys_id IS DISTINCT FROM OLD.task_sys_id) THEN
    RAISE EXCEPTION 'a service principal request''s task is recorded once';
  END IF;
  IF OLD.task_number IS NULL AND NEW.task_number IS NOT NULL AND pair <> 'pending>pending' THEN
    RAISE EXCEPTION 'a task is recorded on a pending request';
  END IF;
  IF pair IN ('pending>approved', 'pending>rejected') AND OLD.task_number IS NULL THEN
    RAISE EXCEPTION 'a pending request is decided by its task';
  END IF;
  IF NEW.close_notes IS DISTINCT FROM OLD.close_notes AND pair <> 'pending>rejected' THEN
    RAISE EXCEPTION 'close notes are kept only when the task rejects the request';
  END IF;
  IF NEW.attempts IS DISTINCT FROM OLD.attempts AND (NEW.attempts <> OLD.attempts + 1 OR NEW.last_error IS NULL) THEN
    RAISE EXCEPTION 'a failure counts one more attempt and keeps its error';
  END IF;
  IF NEW.attempts = OLD.attempts AND NEW.last_error IS DISTINCT FROM OLD.last_error THEN
    RAISE EXCEPTION 'an error is recorded with one more attempt';
  END IF;
  IF pair = 'approved>created' AND NOT EXISTS (SELECT 1 FROM service_principal WHERE request_id = NEW.id) THEN
    RAISE EXCEPTION 'a request is created only with its service principal';
  END IF;
  IF NEW.note_sent_at IS DISTINCT FROM OLD.note_sent_at AND (OLD.note_sent_at IS NOT NULL OR NEW.note_sent_at IS NULL
     OR pair <> 'created>created' OR NEW.task_number IS NULL) THEN
    RAISE EXCEPTION 'the creation note is marked sent once, on a created request with a task';
  END IF;
  RETURN NEW;
END
$$;
DROP TRIGGER request_update ON request;
CREATE TRIGGER request_update BEFORE UPDATE ON request FOR EACH ROW
  WHEN (OLD.kind <> 'service_principal' AND NEW.kind <> 'service_principal') EXECUTE FUNCTION request_update_guard();
CREATE TRIGGER request_update_service_principal BEFORE UPDATE ON request FOR EACH ROW
  WHEN (OLD.kind = 'service_principal' OR NEW.kind = 'service_principal') EXECUTE FUNCTION request_update_service_principal_guard();
DROP TRIGGER request_update_floor ON request;
CREATE TRIGGER request_update_floor BEFORE UPDATE ON request FOR EACH ROW WHEN (NEW.kind <> 'service_principal')
  EXECUTE FUNCTION request_approval_floor_guard();

-- 7b. The collector's UPDATE on request (section 9) is for this kind only: 0012's guard lets a role that is neither the web nor the
-- writer make any of their moves, so without this the collector could move a registration, a binding or a lifecycle request. The
-- trigger's name sorts before request_update's, so it answers first.
CREATE FUNCTION request_collector_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF current_user = 'portal_collector' THEN
    RAISE EXCEPTION 'the collector moves only service principal requests';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER request_collector BEFORE UPDATE ON request FOR EACH ROW WHEN (OLD.kind <> 'service_principal')
  EXECUTE FUNCTION request_collector_guard();

-- 7c. A ServiceNow task is one request's: a lifecycle request's or a service principal request's, never both. A lifecycle request's
-- completion closes its task Closed Complete, and a service principal request whose task closes complete is approved, so a lifecycle
-- request citing a service principal request's task (or the collector recording a lifecycle request's task on one) would approve
-- scopes nobody approved. Same number or same sys_id is refused. Both take the task's lock for their transaction (two keys, the first
-- apart from the name's, so neither meets the other or the collector's one-key session locks). SECURITY DEFINER: the collector may
-- not read a lifecycle request's final.
CREATE FUNCTION request_task_once_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  number text := CASE WHEN NEW.kind = 'lifecycle' THEN NEW.final->>'task' ELSE NEW.task_number END;
  sys_id text := CASE WHEN NEW.kind = 'lifecycle' THEN NEW.final->>'task_sys_id' ELSE NEW.task_sys_id END;
BEGIN
  PERFORM pg_advisory_xact_lock(1735552622, hashtext(coalesce(number, '')));
  IF NEW.kind = 'lifecycle' AND EXISTS (SELECT 1 FROM request WHERE kind = 'service_principal'
                                          AND (task_number = number OR task_sys_id = sys_id)) THEN
    RAISE EXCEPTION 'task % is another request''s task', number;
  END IF;
  IF NEW.kind = 'service_principal' AND EXISTS (SELECT 1 FROM request WHERE kind = 'lifecycle'
                                                  AND (final->>'task' = number OR final->>'task_sys_id' = sys_id)) THEN
    RAISE EXCEPTION 'task % is another request''s task', number;
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION request_task_once_guard() FROM PUBLIC;
CREATE TRIGGER request_task_once_lifecycle BEFORE INSERT ON request FOR EACH ROW WHEN (NEW.kind = 'lifecycle')
  EXECUTE FUNCTION request_task_once_guard();
CREATE TRIGGER request_task_once_service_principal BEFORE UPDATE OF task_number, task_sys_id ON request FOR EACH ROW
  WHEN (NEW.kind = 'service_principal' AND NEW.task_number IS NOT NULL AND OLD.task_number IS NULL)
  EXECUTE FUNCTION request_task_once_guard();

-- 8. 0010's collector audit guard, whole, with the collector's own entries about a service principal request added (the lines marked
-- "0013"). Its search path stays pinned (0012 section 8): CREATE OR REPLACE replaces the SET clause too, so it is given again.
CREATE OR REPLACE FUNCTION audit_log_collector_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF current_user = 'portal_collector' AND NEW.subject_kind = 'request' THEN  -- 0013
    IF NEW.actor_kind <> 'system' OR NEW.actor_id IS NOT NULL  -- 0013
       OR NEW.action NOT IN ('service_principal.task_opened', 'service_principal.approved', 'service_principal.rejected',  -- 0013
                             'service_principal.created')  -- 0013
       OR NOT EXISTS (SELECT 1 FROM request WHERE id = NEW.subject_id AND kind = 'service_principal') THEN  -- 0013
      RAISE EXCEPTION 'the collector writes only system entries about service principal requests';  -- 0013
    END IF;  -- 0013
    RETURN NEW;  -- 0013
  END IF;  -- 0013
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

-- 9. Who does what. The web: asks for a request (an INSERT it already may), reads the principals and the requests' view, and records a
-- mock decision. The writer: reads the principals (the recheck's overlay, S8). The collector: reads the view, the principals and the
-- decisions, moves the kind's columns, and inserts a principal.
GRANT SELECT ON service_principal TO portal_web, portal_writer, portal_collector;
GRANT INSERT ON service_principal TO portal_collector;
GRANT SELECT ON service_principal_request TO portal_web, portal_collector;
GRANT SELECT, INSERT ON mock_task_decision TO portal_web;
GRANT SELECT ON mock_task_decision TO portal_collector;
GRANT UPDATE (state, updated_at, task_number, task_sys_id, attempts, last_error, close_notes, note_sent_at) ON request TO portal_collector;
