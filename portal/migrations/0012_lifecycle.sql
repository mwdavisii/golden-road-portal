-- 0012: lifecycle (builder 0.7.0, spec 2026-11-02). A portal admin's suspend, resume or decommission is a request of kind lifecycle that
-- starts approved (L1: the decision is ServiceNow's); its steps (the deploy repo PR, the archive, the factory PR), the retry, abandon and
-- stop commands the web asks the writer for, the state the platform applied, and the outbox of work notes the collector sends to the task.
-- After a decommission no request is taken for the repo (rule 4). Forward-only. Run as portal_owner by python -m portal.migrate.

-- 1. The kind.
ALTER TABLE request DROP CONSTRAINT request_kind_check;
ALTER TABLE request ADD CONSTRAINT request_kind_check CHECK (kind IN ('registration', 'binding', 'lifecycle'));

-- 2. What the platform applied to each app (no row: active, never acted on). Written by the writer; decommissioned is for good.
CREATE TABLE lifecycle_state (
  repo text PRIMARY KEY CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  state text NOT NULL CHECK (state IN ('active', 'suspended', 'decommissioned')),
  task text CHECK (task ~ '^SCTASK[0-9]{7}$'),
  since timestamptz NOT NULL,
  request text NOT NULL REFERENCES request (id),
  CHECK ((state = 'active') = (task IS NULL))
);
CREATE UNIQUE INDEX lifecycle_state_repo_any_case ON lifecycle_state (lower(repo));

-- A state is the one its request applies (STATE_AFTER, store/base.py): the request's repo (any case), the state its action applies, and
-- its task (none for active), set while the writer is applying it. The one other write: the owner bringing an app back (below).
CREATE FUNCTION lifecycle_state_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  req record;
  brought_back boolean := false;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'lifecycle_state is never deleted'; END IF;
  IF TG_OP = 'UPDATE' THEN
    IF lower(NEW.repo) IS DISTINCT FROM lower(OLD.repo) THEN RAISE EXCEPTION 'a lifecycle state keeps its repo'; END IF;
    -- decommissioned is for good for every process role; the platform team brings an app back by hand, as the schema owner
    -- (docs/roles/platform-team.md procedure 18; decided while planning: item 20), and only once the request that decommissioned it
    -- is over: applied in git, or stopped (the writer never archives or marks the record of an app brought back)
    IF OLD.state = 'decommissioned' THEN
      IF NOT (NEW.state = 'active' AND NEW.task IS NULL AND NEW.request = OLD.request AND current_user = 'portal_owner') THEN
        RAISE EXCEPTION 'the app is decommissioned for good';
      END IF;
      IF NOT EXISTS (SELECT 1 FROM request WHERE id = OLD.request AND (state = 'merged' OR (state = 'failed' AND failure_reason = 'stopped'))) THEN
        RAISE EXCEPTION 'an app is brought back only once its decommission is applied in git or stopped';
      END IF;
      brought_back := true;
    END IF;
  END IF;
  SELECT kind, repo, state, final INTO req FROM request WHERE id = NEW.request;
  IF req.kind IS DISTINCT FROM 'lifecycle' THEN
    RAISE EXCEPTION 'a lifecycle state names the lifecycle request that set it';
  END IF;
  IF NOT brought_back THEN
    IF lower(NEW.repo) <> lower(req.repo)
       OR NEW.state IS DISTINCT FROM (CASE req.final->>'action' WHEN 'suspend' THEN 'suspended' WHEN 'resume' THEN 'active'
                                                             WHEN 'decommission' THEN 'decommissioned' END)
       OR NEW.task IS DISTINCT FROM (CASE WHEN NEW.state = 'active' THEN NULL ELSE req.final->>'task' END) THEN
      RAISE EXCEPTION 'a lifecycle state is the one its request applies: its repo, its action''s state and its task';
    END IF;
    IF req.state <> 'writing' THEN RAISE EXCEPTION 'a lifecycle state is set only while its request is being written'; END IF;
  END IF;
  NEW.since := now();
  RETURN NEW;
END
$$;
CREATE TRIGGER lifecycle_state_write BEFORE INSERT OR UPDATE OR DELETE ON lifecycle_state FOR EACH ROW EXECUTE FUNCTION lifecycle_state_guard();
-- A row trigger doesn't see a TRUNCATE: none of the four lifecycle tables is ever emptied, the owner included.
CREATE FUNCTION lifecycle_never_truncated() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  RAISE EXCEPTION '% is never deleted', TG_TABLE_NAME;
END
$$;
CREATE TRIGGER lifecycle_state_no_truncate BEFORE TRUNCATE ON lifecycle_state FOR EACH STATEMENT EXECUTE FUNCTION lifecycle_never_truncated();

-- 3. A lifecycle request's steps, in rule 7's order (ACTION_STEPS, store/base.py: deploy, archive, factory). Only the writer writes them,
-- and only while the request is writing; each move is one of STEP_TRANSITIONS; a step leaves planned (but for an abandon's closed) only
-- once every earlier step is done; a step's pull request is recorded when it opens and kept as it is until a retry or rebuild.
CREATE TABLE lifecycle_step (
  request_id text NOT NULL REFERENCES request (id),
  step text NOT NULL CHECK (step IN ('deploy', 'factory', 'archive')),
  position smallint NOT NULL CHECK (position BETWEEN 1 AND 3),
  state text NOT NULL CHECK (state IN ('planned', 'pr_open', 'done', 'failed', 'closed')),
  attempt integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  branch text CHECK (branch ~ '^[A-Za-z0-9._/-]{1,200}$'),
  pr_number integer CHECK (pr_number > 0),
  pr_url text CHECK (length(pr_url) <= 400),
  head_sha text CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  base_sha text CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  merge_sha text CHECK (merge_sha ~ '^[0-9a-f]{40}$'),
  failure_reason text CHECK (length(failure_reason) BETWEEN 1 AND 300),
  updated_at timestamptz NOT NULL,
  PRIMARY KEY (request_id, step),
  UNIQUE (request_id, position),
  CHECK (state <> 'pr_open' OR (branch IS NOT NULL AND pr_number IS NOT NULL AND pr_url IS NOT NULL AND head_sha IS NOT NULL AND base_sha IS NOT NULL)),
  CHECK ((state = 'failed') = (failure_reason IS NOT NULL)),
  CHECK (state <> 'planned' OR (branch IS NULL AND pr_number IS NULL AND pr_url IS NULL AND head_sha IS NULL AND base_sha IS NULL
                                AND merge_sha IS NULL))
);

CREATE FUNCTION lifecycle_step_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  pair text;
  legal text[] := ARRAY['planned>pr_open', 'planned>done', 'pr_open>done', 'planned>failed', 'pr_open>failed', 'pr_open>planned',
                        'failed>planned', 'planned>planned', 'planned>closed', 'pr_open>closed', 'failed>closed'];
  req record;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'a lifecycle step is never deleted'; END IF;
  SELECT kind, state INTO req FROM request WHERE id = NEW.request_id;
  IF req.kind IS DISTINCT FROM 'lifecycle' THEN
    RAISE EXCEPTION 'a lifecycle step belongs to a lifecycle request';
  END IF;
  IF req.state <> 'writing' THEN RAISE EXCEPTION 'a lifecycle step is planned or moved only while its request is being written'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'planned' OR NEW.attempt <> 1 THEN RAISE EXCEPTION 'a lifecycle step starts planned'; END IF;
  ELSE
    pair := OLD.state || '>' || NEW.state;
    IF NOT pair = ANY (legal) THEN RAISE EXCEPTION 'illegal lifecycle step transition %', pair; END IF;
    IF NEW.request_id IS DISTINCT FROM OLD.request_id OR NEW.step IS DISTINCT FROM OLD.step OR NEW.position IS DISTINCT FROM OLD.position THEN
      RAISE EXCEPTION 'a lifecycle step keeps its request, name and position';
    END IF;
    IF NEW.attempt IS DISTINCT FROM (CASE WHEN NEW.state = 'planned' THEN OLD.attempt + 1 ELSE OLD.attempt END) THEN
      RAISE EXCEPTION 'only a retry or a rebuild starts a new attempt';
    END IF;
    IF NEW.state NOT IN ('pr_open', 'planned') AND (NEW.branch IS DISTINCT FROM OLD.branch OR NEW.pr_number IS DISTINCT FROM OLD.pr_number
       OR NEW.pr_url IS DISTINCT FROM OLD.pr_url OR NEW.head_sha IS DISTINCT FROM OLD.head_sha OR NEW.base_sha IS DISTINCT FROM OLD.base_sha) THEN
      RAISE EXCEPTION 'a step''s pull request is recorded when it opens and never changes after';
    END IF;
    IF OLD.state = 'planned' AND NEW.state <> 'closed' AND EXISTS (SELECT 1 FROM lifecycle_step s WHERE s.request_id = NEW.request_id
                                                                   AND s.position < NEW.position AND s.state <> 'done') THEN
      RAISE EXCEPTION 'a lifecycle step leaves planned only after every earlier step is done';
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END
$$;
CREATE TRIGGER lifecycle_step_write BEFORE INSERT OR UPDATE OR DELETE ON lifecycle_step FOR EACH ROW EXECUTE FUNCTION lifecycle_step_guard();
CREATE TRIGGER lifecycle_step_no_truncate BEFORE TRUNCATE ON lifecycle_step FOR EACH STATEMENT EXECUTE FUNCTION lifecycle_never_truncated();

-- 4. Retry, abandon and stop, asked by an admin in the web, carried out by the writer. One open per request.
CREATE TABLE lifecycle_command (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id text NOT NULL REFERENCES request (id),
  command text NOT NULL CHECK (command IN ('retry', 'abandon', 'stop')),
  requested_by text NOT NULL REFERENCES person (id),
  requested_at timestamptz NOT NULL,
  done_at timestamptz
);
CREATE UNIQUE INDEX lifecycle_command_one_open ON lifecycle_command (request_id) WHERE done_at IS NULL;

CREATE FUNCTION lifecycle_command_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'a lifecycle command is never deleted'; END IF;
  IF TG_OP = 'INSERT' THEN
    -- before the foreign key: an unknown request is not_found, as in the MemoryStore
    IF NOT EXISTS (SELECT 1 FROM request WHERE id = NEW.request_id) THEN RAISE EXCEPTION 'a lifecycle command names no such request'; END IF;
    IF (SELECT kind FROM request WHERE id = NEW.request_id) IS DISTINCT FROM 'lifecycle' THEN
      RAISE EXCEPTION 'a lifecycle command is for a lifecycle request';
    END IF;
    NEW.requested_at := now();
    NEW.done_at := NULL;
  ELSE
    IF OLD.done_at IS NOT NULL OR NEW.done_at IS NULL OR NEW.id <> OLD.id OR NEW.request_id <> OLD.request_id OR NEW.command <> OLD.command
       OR NEW.requested_by <> OLD.requested_by OR NEW.requested_at <> OLD.requested_at THEN
      RAISE EXCEPTION 'a lifecycle command is only marked done, once';
    END IF;
    NEW.done_at := now();
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER lifecycle_command_write BEFORE INSERT OR UPDATE OR DELETE ON lifecycle_command FOR EACH ROW EXECUTE FUNCTION lifecycle_command_guard();
CREATE TRIGGER lifecycle_command_no_truncate BEFORE TRUNCATE ON lifecycle_command FOR EACH STATEMENT EXECUTE FUNCTION lifecycle_never_truncated();

-- 5. The outbox (L11): one work note per step, sent by the collector in id order per request (a note is never marked sent while an
-- earlier one of its request is unsent). The note's task is the request's own, set here whatever an INSERT says (SECURITY DEFINER: the
-- collector can't read request.final). Each process role adds only its own steps, by session_user (inside SECURITY DEFINER current_user
-- is the owner): the web only requested, the collector only ci_corrected (rule 9), the writer every other one (completed closes the
-- task). A request has one requested note and one completed note.
CREATE TABLE lifecycle_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_id text NOT NULL REFERENCES request (id),
  step text NOT NULL CHECK (step IN ('requested', 'pr_opened', 'merged', 'factory_merged', 'archived', 'failed', 'retried', 'abandoned',
                                     'completed', 'ci_corrected', 'command', 'rebuilt', 'stopped')),
  note text NOT NULL CHECK (length(note) BETWEEN 1 AND 3000),
  task_sys_id text NOT NULL CHECK (task_sys_id ~ '^[0-9a-f]{32}$'),
  created_at timestamptz NOT NULL,
  sent_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error text CHECK (length(last_error) BETWEEN 1 AND 300)
);
CREATE INDEX lifecycle_events_unsent ON lifecycle_events (request_id, id) WHERE sent_at IS NULL;
CREATE UNIQUE INDEX lifecycle_events_one_requested ON lifecycle_events (request_id) WHERE step = 'requested';
CREATE UNIQUE INDEX lifecycle_events_one_completed ON lifecycle_events (request_id) WHERE step = 'completed';

CREATE FUNCTION lifecycle_events_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  req record;
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'lifecycle_events is never deleted'; END IF;
  IF TG_OP = 'INSERT' THEN
    SELECT kind, final->>'task_sys_id' AS task INTO req FROM request WHERE id = NEW.request_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'a lifecycle event names no such request'; END IF;
    IF req.kind IS DISTINCT FROM 'lifecycle' THEN RAISE EXCEPTION 'a lifecycle event is for a lifecycle request'; END IF;
    IF (session_user = 'portal_web' AND NEW.step <> 'requested') OR (session_user = 'portal_collector' AND NEW.step <> 'ci_corrected')
       OR (session_user = 'portal_writer' AND NEW.step IN ('requested', 'ci_corrected')) THEN
      RAISE EXCEPTION 'a lifecycle event of step % isn''t this role''s to add', NEW.step;
    END IF;
    NEW.task_sys_id := req.task;
    NEW.created_at := now();
    NEW.sent_at := NULL;
    NEW.attempts := 0;
    NEW.last_error := NULL;
  ELSE
    IF NEW.id <> OLD.id OR NEW.request_id <> OLD.request_id OR NEW.step <> OLD.step OR NEW.note <> OLD.note
       OR NEW.task_sys_id <> OLD.task_sys_id OR NEW.created_at <> OLD.created_at THEN
      RAISE EXCEPTION 'a lifecycle event''s request, step, note, task and time never change';
    END IF;
    IF OLD.sent_at IS NOT NULL THEN RAISE EXCEPTION 'a lifecycle event is sent once'; END IF;
    IF NEW.sent_at IS NOT NULL THEN
      IF EXISTS (SELECT 1 FROM lifecycle_events e WHERE e.request_id = OLD.request_id AND e.id < OLD.id AND e.sent_at IS NULL) THEN
        RAISE EXCEPTION 'a lifecycle event is sent only after every earlier one of its request';
      END IF;
      NEW.sent_at := now();
      NEW.attempts := OLD.attempts;
      NEW.last_error := OLD.last_error;
    ELSIF NEW.attempts <> OLD.attempts + 1 OR NEW.last_error IS NULL THEN
      RAISE EXCEPTION 'a failed send counts one more attempt and keeps its error';
    END IF;
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION lifecycle_events_guard() FROM PUBLIC;
CREATE TRIGGER lifecycle_events_write BEFORE INSERT OR UPDATE OR DELETE ON lifecycle_events FOR EACH ROW EXECUTE FUNCTION lifecycle_events_guard();
CREATE TRIGGER lifecycle_events_no_truncate BEFORE TRUNCATE ON lifecycle_events FOR EACH STATEMENT EXECUTE FUNCTION lifecycle_never_truncated();

-- 6. 0011's insert guard, whole, with the lifecycle kind and rule 4's refusal added (the lines marked "0012").
CREATE OR REPLACE FUNCTION request_insert_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL
     OR NEW.failure_reason IS NOT NULL OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL OR NEW.submission <> 1
     OR NEW.floor_override IS NOT NULL OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending (a binding: approved) with no decision or result';
  END IF;
  IF EXISTS (SELECT 1 FROM lifecycle_state WHERE lower(repo) = lower(NEW.repo) AND state = 'decommissioned') THEN  -- 0012
    RAISE EXCEPTION 'the app is decommissioned: no request is taken for it';  -- 0012
  END IF;  -- 0012
  IF NEW.kind = 'binding' THEN
    IF NEW.state <> 'approved' OR NEW.final IS NULL OR NEW.final IS DISTINCT FROM NEW.draft
       OR NEW.final->>'schema' IS DISTINCT FROM 'binding/v2' OR NEW.final->>'request' IS DISTINCT FROM NEW.id
       OR NEW.final->>'linked_by' IS DISTINCT FROM NEW.requested_by OR NEW.final->>'repo' IS DISTINCT FROM NEW.repo
       OR NEW.final->>'environment' IS NULL OR NEW.final->>'environment' NOT IN ('test', 'prod') THEN
      RAISE EXCEPTION 'a binding request starts approved, its record is its draft, and it names its request, person, repo and environment';
    END IF;
  ELSIF NEW.kind = 'lifecycle' THEN  -- 0012
    IF NEW.state <> 'approved' OR NEW.final IS NULL OR NEW.final IS DISTINCT FROM NEW.draft OR jsonb_typeof(NEW.final) <> 'object'
       OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(NEW.final) AS k) IS DISTINCT FROM ARRAY['action', 'reason', 'task', 'task_sys_id']
       OR NEW.final->>'action' NOT IN ('suspend', 'resume', 'decommission')
       OR coalesce(NEW.final->>'task', '') !~ '^SCTASK[0-9]{7}$' OR coalesce(NEW.final->>'task_sys_id', '') !~ '^[0-9a-f]{32}$'
       OR length(coalesce(NEW.final->>'reason', '')) NOT BETWEEN 10 AND 1000 OR NEW.final->>'reason' ~ '[\r\n]' THEN
      RAISE EXCEPTION 'a lifecycle request starts approved, and its record is its draft: an action, a task, its sys_id and a one-line reason';
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

-- 7. 0004's update guard, whole (no later migration replaced it), with the lifecycle request's moves added (the lines marked "0012"):
-- the writer applies it without a pull request of its own (writing -> merged), and it never goes pr_open.
CREATE OR REPLACE FUNCTION request_update_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  pair text := OLD.state || '>' || NEW.state;
  web_pairs text[] := ARRAY['pending>approved', 'pending>sent_back', 'sent_back>pending'];
  writer_pairs text[] := ARRAY['approved>writing', 'writing>pr_open', 'pr_open>merged', 'writing>failed', 'pr_open>failed',
                                'writing>merged'];  -- 0012
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.repo IS DISTINCT FROM OLD.repo
     OR NEW.requested_by IS DISTINCT FROM OLD.requested_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'a request''s id, kind, repo, requester and creation time never change';
  END IF;
  IF pair = 'sent_back>pending' THEN
    IF NEW.submission <> OLD.submission + 1 OR NEW.floor_override IS NOT NULL THEN
      RAISE EXCEPTION 'a resubmission counts up and starts without an override';
    END IF;
  ELSIF NEW.submission IS DISTINCT FROM OLD.submission THEN
    RAISE EXCEPTION 'only a resubmission changes the submission';
  END IF;
  IF current_user = 'portal_web' THEN
    IF NOT pair = ANY (web_pairs) THEN RAISE EXCEPTION 'illegal request transition % for portal_web', pair; END IF;
  ELSIF current_user = 'portal_writer' THEN
    IF NOT pair = ANY (writer_pairs) THEN RAISE EXCEPTION 'illegal request transition % for portal_writer', pair; END IF;
  ELSIF NOT (pair = ANY (web_pairs) OR pair = ANY (writer_pairs)) THEN
    RAISE EXCEPTION 'illegal request transition %', pair;
  END IF;
  IF NEW.kind = 'lifecycle' AND NEW.state = 'pr_open' THEN  -- 0012
    RAISE EXCEPTION 'a lifecycle request keeps its pull requests on its steps';  -- 0012
  END IF;  -- 0012
  IF pair = 'writing>merged' AND NEW.kind <> 'lifecycle' THEN  -- 0012
    RAISE EXCEPTION 'only a lifecycle request is applied without its own pull request';  -- 0012
  END IF;  -- 0012
  IF pair = 'writing>merged' THEN  -- 0012: applied once every step is done and the state it applies is set
    IF NOT EXISTS (SELECT 1 FROM lifecycle_step WHERE request_id = NEW.id)  -- 0012
       OR EXISTS (SELECT 1 FROM lifecycle_step WHERE request_id = NEW.id AND state <> 'done')  -- 0012
       OR NOT EXISTS (SELECT 1 FROM lifecycle_state WHERE lower(repo) = lower(NEW.repo) AND request = NEW.id) THEN  -- 0012
      RAISE EXCEPTION 'a lifecycle request is applied only once every one of its steps is done and its state is set';  -- 0012
    END IF;  -- 0012
  END IF;  -- 0012

  IF pair IN ('pending>approved', 'pending>sent_back') THEN
    IF NEW.draft IS DISTINCT FROM OLD.draft THEN RAISE EXCEPTION 'a decision does not change the draft'; END IF;
    IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL OR NEW.decision_groups IS NULL OR coalesce(NEW.decision_note, '') = '' THEN
      RAISE EXCEPTION 'a decision records who, when, their groups and why';
    END IF;
    IF pair = 'pending>approved' AND NEW.final IS NULL THEN RAISE EXCEPTION 'an approval records the final record'; END IF;
    IF pair = 'pending>sent_back' AND NEW.final IS NOT NULL THEN RAISE EXCEPTION 'a request sent back has no final record'; END IF;
    IF pair = 'pending>sent_back' AND NEW.floor_override IS NOT NULL THEN RAISE EXCEPTION 'a request sent back has no override'; END IF;
    IF NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL THEN RAISE EXCEPTION 'a decision records no commits'; END IF;
  ELSIF pair = 'sent_back>pending' THEN
    IF NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL OR NEW.decision_note IS NOT NULL
       OR NEW.final IS NOT NULL THEN
      RAISE EXCEPTION 'a resubmitted request starts without a decision';
    END IF;
  ELSE
    -- the writer's steps: the draft, the final record and the decision of an approved request never change, and once a pull
    -- request is open neither do the commits the merge is pinned to
    IF NEW.draft IS DISTINCT FROM OLD.draft OR NEW.final IS DISTINCT FROM OLD.final OR NEW.decided_by IS DISTINCT FROM OLD.decided_by
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at OR NEW.decision_groups IS DISTINCT FROM OLD.decision_groups
       OR NEW.decision_note IS DISTINCT FROM OLD.decision_note THEN
      RAISE EXCEPTION 'the draft, the final record and the decision of an approved request never change';
    END IF;
    IF NEW.floor_override IS DISTINCT FROM OLD.floor_override THEN
      RAISE EXCEPTION 'the override of an approved request never changes';
    END IF;
    IF pair = 'writing>pr_open' THEN
      IF NEW.branch IS NULL OR NEW.pr_number IS NULL OR NEW.pr_url IS NULL THEN
        RAISE EXCEPTION 'an open pull request records its branch, number and URL';
      END IF;
      IF NEW.head_sha IS NULL OR NEW.base_sha IS NULL THEN
        RAISE EXCEPTION 'an open pull request records its head and base commits';
      END IF;
    ELSIF NEW.head_sha IS DISTINCT FROM OLD.head_sha OR NEW.base_sha IS DISTINCT FROM OLD.base_sha OR NEW.branch IS DISTINCT FROM OLD.branch
          OR NEW.pr_number IS DISTINCT FROM OLD.pr_number OR NEW.pr_url IS DISTINCT FROM OLD.pr_url THEN
      RAISE EXCEPTION 'the branch, pull request and commits of an open pull request never change';
    END IF;
    IF pair = 'pr_open>merged' AND NEW.merge_sha IS NULL THEN RAISE EXCEPTION 'a merge records its commit'; END IF;
    IF NEW.state = 'failed' AND coalesce(NEW.failure_reason, '') = '' THEN RAISE EXCEPTION 'a failure records its reason'; END IF;
  END IF;
  RETURN NEW;
END
$$;

-- 8. Every trigger function pins its search path, pg_temp last (the 0012 ones above, and here the older ones). Unpinned, pg_temp is
-- searched first: any role may CREATE TEMP TABLE, and a temp table named like one a guard reads (lifecycle_state, registration_evidence,
-- request, ...) would answer the guard instead, in that role's session.
ALTER FUNCTION audit_log_is_append_only() SET search_path = public, pg_temp;
ALTER FUNCTION registration_evidence_insert_guard() SET search_path = public, pg_temp;
ALTER FUNCTION registration_evidence_is_insert_only() SET search_path = public, pg_temp;
ALTER FUNCTION request_approval_floor_guard() SET search_path = public, pg_temp;
ALTER FUNCTION rereview_is_insert_only() SET search_path = public, pg_temp;
ALTER FUNCTION rereview_times() SET search_path = public, pg_temp;
ALTER FUNCTION rereview_item_event_guard() SET search_path = public, pg_temp;
ALTER FUNCTION rereview_item_close_guard() SET search_path = public, pg_temp;
ALTER FUNCTION audit_log_collector_guard() SET search_path = public, pg_temp;
ALTER FUNCTION cmdb_record_times() SET search_path = public, pg_temp;

-- 9. Who does what. The web: reads everything here, asks for a request (its first note) and a command. The writer: the state, the steps,
-- marking a command done, and every later note. The collector: reads the state, sends the notes (rule 9's note is its own).
GRANT SELECT ON lifecycle_state, lifecycle_step, lifecycle_command, lifecycle_events TO portal_web;
GRANT INSERT (request_id, command, requested_by) ON lifecycle_command TO portal_web;
GRANT INSERT (request_id, step, note) ON lifecycle_events TO portal_web;
GRANT SELECT, INSERT, UPDATE ON lifecycle_state, lifecycle_step TO portal_writer;
GRANT SELECT ON lifecycle_command TO portal_writer;
GRANT UPDATE (done_at) ON lifecycle_command TO portal_writer;
GRANT SELECT ON lifecycle_events TO portal_writer;
GRANT INSERT (request_id, step, note) ON lifecycle_events TO portal_writer;
GRANT SELECT ON lifecycle_state TO portal_collector;
GRANT SELECT ON lifecycle_events TO portal_collector;
GRANT INSERT (request_id, step, note) ON lifecycle_events TO portal_collector;
GRANT UPDATE (sent_at, attempts, last_error) ON lifecycle_events TO portal_collector;
