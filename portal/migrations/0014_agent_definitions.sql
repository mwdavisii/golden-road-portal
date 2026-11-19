-- 0014: build an agent (builder 0.8.0, spec 2026-11-17). A request of kind agent_definition starts approved (no admin approval: the
-- PR's review and checks are the gate), its record its draft (an agent/v1 document). Several may be open per repo; the writer fails an
-- older one as superseded. Every later move is the writer's: claim it, count a failure it retries (no cap), open or update the PR on
-- portal/agent-definition, record the merge a person makes, or fail it. The kind has its own insert and update triggers (0002's,
-- 0005's and 0012's guards skip it, as they skip 0013's kind). Also: agent_observed (the collector's view of agent repos) and two
-- evidence problems. The newest definition wins (review I1): an older one is never claimed or opened once a newer one is open for its
-- repo, and only a newer open one supersedes. Each insert and each of those moves takes the repo's transaction lock first, two keys:
-- 0014's namespace 1735552623 (0x67726E6F, "grno") and the repo's hash, so an insert and a move of one repo see each other. A
-- two-key lock never meets a one-key one (the collector's session locks SP_LOCK and WRITEBACK_LOCK), and 1735552623 is apart from
-- 0013's 1735552621 (a service principal's name) and 1735552622 (a ServiceNow task). Forward-only. Run as portal_owner by python -m
-- portal.migrate.

-- 1. The kind, and the two 0013 columns it keeps (attempts, last_error); the other four stay the service principal kind's.
ALTER TABLE request DROP CONSTRAINT request_kind_check;
ALTER TABLE request ADD CONSTRAINT request_kind_check
  CHECK (kind IN ('registration', 'binding', 'lifecycle', 'service_principal', 'agent_definition'));
ALTER TABLE request DROP CONSTRAINT request_service_principal_columns;
ALTER TABLE request ADD CONSTRAINT request_service_principal_columns CHECK (kind = 'service_principal' OR (task_number IS NULL
  AND task_sys_id IS NULL AND close_notes IS NULL AND note_sent_at IS NULL));
ALTER TABLE request ADD CONSTRAINT request_attempt_columns
  CHECK (kind IN ('service_principal', 'agent_definition') OR (attempts = 0 AND last_error IS NULL));

-- 2. One open request per kind and repo, as 0013; an agent definition is keyed by its own id (several may be open).
DROP INDEX request_one_open;
CREATE UNIQUE INDEX request_one_open ON request (kind, lower(repo), (CASE WHEN kind = 'binding' THEN coalesce(draft->>'environment', 'test')
  WHEN kind IN ('service_principal', 'agent_definition') THEN id ELSE '' END))
  WHERE state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open');

-- 3. An agent/v1 document's shape, for a definition's draft and an observed agent.yaml: its keys, each field's type and length, the
-- model's pattern, no control character (the instructions keep tab and line feed; a one-line field no line or paragraph separator
-- either), no blank text, at most four starters. The stores check first with agent_file (Python's rules, the platform's models): this
-- is the backstop. Its blank characters are exactly what Python's \s matches (test_the_database_s_blank_characters_are_python_s).
CREATE FUNCTION agent_v1_shape(doc jsonb) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = public, pg_temp AS $$
DECLARE
  keys text[];
  one_line constant text := '[\u0001-\u001f\u007f-\u009f\u2028\u2029]';
  text_control constant text := '[\u0001-\u0008\u000b-\u001f\u007f-\u009f]';
  blank constant text := '^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028-\u2029\u202f\u205f\u3000]*$';
BEGIN
  IF jsonb_typeof(doc) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
  keys := coalesce((SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(doc) AS k), '{}'::text[]);
  RETURN coalesce(keys <@ ARRAY['display_name', 'instructions', 'model', 'schema', 'starters']
     AND ARRAY['display_name', 'instructions', 'model', 'schema'] <@ keys
     AND jsonb_typeof(doc->'schema') = 'string' AND doc->>'schema' = 'agent/v1'
     AND jsonb_typeof(doc->'display_name') = 'string' AND length(doc->>'display_name') BETWEEN 1 AND 64
     AND doc->>'display_name' !~ blank AND doc->>'display_name' !~ one_line
     AND jsonb_typeof(doc->'model') = 'string' AND doc->>'model' ~ '^[a-z0-9][a-z0-9._-]{0,63}$'
     AND jsonb_typeof(doc->'instructions') = 'string' AND length(doc->>'instructions') BETWEEN 1 AND 8000
     AND doc->>'instructions' !~ blank AND doc->>'instructions' !~ text_control
     AND (NOT doc ? 'starters' OR (jsonb_typeof(doc->'starters') = 'array' AND jsonb_array_length(doc->'starters') <= 4
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(doc->'starters') AS e(v)
                          WHERE jsonb_typeof(e.v) <> 'string' OR length(e.v #>> '{}') NOT BETWEEN 1 AND 200
                             OR (e.v #>> '{}') ~ blank OR (e.v #>> '{}') ~ one_line))), false);
END
$$;
REVOKE ALL ON FUNCTION agent_v1_shape(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION agent_v1_shape(jsonb) TO portal_web, portal_collector;

-- 3b. The kind's own insert check: approved, its record its draft, an agent/v1's shape, no later field set, not dated in the future
-- (a definition dated ahead would stay the newest), and the repo's lock (see the header).
CREATE FUNCTION request_insert_agent_definition_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
BEGIN
  IF NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL
     OR NEW.failure_reason IS NOT NULL OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL OR NEW.submission <> 1
     OR NEW.floor_override IS NOT NULL OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL OR NEW.ownership_acknowledged_at IS NOT NULL
     OR NEW.task_number IS NOT NULL OR NEW.task_sys_id IS NOT NULL OR NEW.attempts <> 0 OR NEW.last_error IS NOT NULL
     OR NEW.close_notes IS NOT NULL OR NEW.note_sent_at IS NOT NULL THEN
    RAISE EXCEPTION 'an agent definition starts with no decision, pull request or result';
  END IF;
  IF NEW.created_at > now() + interval '5 minutes' THEN
    RAISE EXCEPTION 'an agent definition is not dated in the future';
  END IF;
  PERFORM pg_advisory_xact_lock(1735552623, hashtext(lower(NEW.repo)));  -- two keys: see the header
  IF EXISTS (SELECT 1 FROM lifecycle_state WHERE lower(repo) = lower(NEW.repo) AND state = 'decommissioned') THEN
    RAISE EXCEPTION 'the app is decommissioned: no request is taken for it';
  END IF;
  IF NEW.state <> 'approved' OR NEW.final IS DISTINCT FROM NEW.draft OR NOT agent_v1_shape(NEW.draft) THEN
    RAISE EXCEPTION 'an agent definition starts approved, its record its draft: an agent/v1 with a name, a model and instructions';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION request_insert_agent_definition_guard() FROM PUBLIC;
DROP TRIGGER request_insert ON request;
CREATE TRIGGER request_insert BEFORE INSERT ON request FOR EACH ROW WHEN (NEW.kind NOT IN ('service_principal', 'agent_definition'))
  EXECUTE FUNCTION request_insert_guard();
CREATE TRIGGER request_insert_agent_definition BEFORE INSERT ON request FOR EACH ROW WHEN (NEW.kind = 'agent_definition')
  EXECUTE FUNCTION request_insert_agent_definition_guard();

-- 4. The kind's moves, the writer's only: claim (approved -> writing, the newest only), count a retried failure (writing or pr_open,
-- one more attempt with its error), open or adopt the PR on portal/agent-definition (writing -> pr_open, the newest only, a PR of its
-- own repo), record a person's merge (pr_open -> merged), or fail it with a reason (writing or pr_open -> failed; superseded, only
-- while a newer one is open, from approved too). Nothing else of the request changes.
-- 0002's request_update trigger (0012's guard) and 0005's request_update_floor now skip the kind.
CREATE FUNCTION request_update_agent_definition_guard() RETURNS trigger LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  pair text := OLD.state || '>' || NEW.state;
  moves text[] := ARRAY['approved>writing', 'writing>writing', 'writing>pr_open', 'pr_open>pr_open', 'pr_open>merged', 'approved>failed',
                        'writing>failed', 'pr_open>failed'];
  newer boolean := false;
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.repo IS DISTINCT FROM OLD.repo
     OR NEW.requested_by IS DISTINCT FROM OLD.requested_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'a request''s id, kind, repo, requester and creation time never change';
  END IF;
  IF current_user NOT IN ('portal_writer', 'portal_owner') THEN  -- an allow-list: a role added later is refused too
    RAISE EXCEPTION 'only the writer moves an agent definition';
  END IF;
  IF NOT pair = ANY (moves) THEN RAISE EXCEPTION 'illegal agent definition transition %', pair; END IF;
  -- the newest wins (review I1): under the repo's lock (see the header), is a newer definition open for this repo?
  IF pair IN ('approved>writing', 'writing>pr_open', 'approved>failed', 'writing>failed', 'pr_open>failed') THEN
    PERFORM pg_advisory_xact_lock(1735552623, hashtext(lower(NEW.repo)));
    newer := EXISTS (SELECT 1 FROM request WHERE kind = 'agent_definition' AND lower(repo) = lower(NEW.repo) AND id <> NEW.id
                       AND state IN ('approved', 'writing', 'pr_open') AND (created_at, id) > (NEW.created_at, NEW.id));
  END IF;
  IF pair IN ('approved>writing', 'writing>pr_open') AND newer THEN
    RAISE EXCEPTION 'a newer agent definition for % is open: it wins, and this one is superseded', NEW.repo;
  END IF;
  IF pair = 'approved>failed' AND NEW.failure_reason IS DISTINCT FROM 'superseded' THEN
    RAISE EXCEPTION 'an approved agent definition fails only as superseded';
  END IF;
  IF NEW.state = 'failed' AND NEW.failure_reason = 'superseded' AND NOT newer THEN
    RAISE EXCEPTION 'an agent definition is superseded only by a newer open one';
  END IF;
  IF NEW.draft IS DISTINCT FROM OLD.draft OR NEW.final IS DISTINCT FROM OLD.final OR NEW.submission IS DISTINCT FROM OLD.submission
     OR NEW.decided_by IS DISTINCT FROM OLD.decided_by OR NEW.decided_at IS DISTINCT FROM OLD.decided_at
     OR NEW.decision_groups IS DISTINCT FROM OLD.decision_groups OR NEW.decision_note IS DISTINCT FROM OLD.decision_note
     OR NEW.floor_override IS DISTINCT FROM OLD.floor_override OR NEW.ownership_acknowledged_at IS DISTINCT FROM OLD.ownership_acknowledged_at
     OR NEW.task_number IS DISTINCT FROM OLD.task_number OR NEW.task_sys_id IS DISTINCT FROM OLD.task_sys_id
     OR NEW.close_notes IS DISTINCT FROM OLD.close_notes OR NEW.note_sent_at IS DISTINCT FROM OLD.note_sent_at THEN
    RAISE EXCEPTION 'an agent definition''s draft, record and decision never change';
  END IF;
  IF pair = 'writing>pr_open' THEN
    IF NEW.branch IS DISTINCT FROM 'portal/agent-definition' OR NEW.pr_number IS NULL OR NEW.pr_url IS NULL OR NEW.head_sha IS NULL
       OR NEW.base_sha IS NULL
       OR NEW.pr_url !~* ('^https://[^/]+/' || replace(NEW.repo, '.', '\.') || '/pull/' || NEW.pr_number || '$') THEN
      RAISE EXCEPTION 'an agent definition''s pull request is on portal/agent-definition, its own repo''s, with its number, URL and commits';
    END IF;
  ELSIF NEW.branch IS DISTINCT FROM OLD.branch OR NEW.pr_number IS DISTINCT FROM OLD.pr_number OR NEW.pr_url IS DISTINCT FROM OLD.pr_url
        OR NEW.head_sha IS DISTINCT FROM OLD.head_sha OR NEW.base_sha IS DISTINCT FROM OLD.base_sha THEN
    RAISE EXCEPTION 'the branch, pull request and commits of an agent definition are recorded once';
  END IF;
  IF (pair = 'pr_open>merged') <> (NEW.merge_sha IS NOT NULL) THEN
    RAISE EXCEPTION 'a merge records its commit, and only a merge does';
  END IF;
  IF NEW.state = 'failed' AND coalesce(NEW.failure_reason, '') = '' THEN RAISE EXCEPTION 'a failure records its reason'; END IF;
  IF NEW.state <> 'failed' AND NEW.failure_reason IS NOT NULL THEN RAISE EXCEPTION 'only a failure records a reason'; END IF;
  IF NEW.attempts IS DISTINCT FROM OLD.attempts AND (NEW.attempts <> OLD.attempts + 1 OR NEW.last_error IS NULL) THEN
    RAISE EXCEPTION 'a failure the writer retries counts one more attempt and keeps its error';
  END IF;
  IF NEW.attempts = OLD.attempts AND NEW.last_error IS DISTINCT FROM OLD.last_error THEN
    RAISE EXCEPTION 'an error is recorded with one more attempt';
  END IF;
  IF pair IN ('writing>writing', 'pr_open>pr_open') AND NEW.attempts = OLD.attempts THEN
    RAISE EXCEPTION 'an agent definition stays where it is only to count a retried failure';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION request_update_agent_definition_guard() FROM PUBLIC;
DROP TRIGGER request_update ON request;
CREATE TRIGGER request_update BEFORE UPDATE ON request FOR EACH ROW
  WHEN (OLD.kind NOT IN ('service_principal', 'agent_definition') AND NEW.kind NOT IN ('service_principal', 'agent_definition'))
  EXECUTE FUNCTION request_update_guard();
CREATE TRIGGER request_update_agent_definition BEFORE UPDATE ON request FOR EACH ROW
  WHEN (OLD.kind = 'agent_definition' OR NEW.kind = 'agent_definition') EXECUTE FUNCTION request_update_agent_definition_guard();
DROP TRIGGER request_update_floor ON request;
CREATE TRIGGER request_update_floor BEFORE UPDATE ON request FOR EACH ROW WHEN (NEW.kind NOT IN ('service_principal', 'agent_definition'))
  EXECUTE FUNCTION request_approval_floor_guard();
-- 0013's request_collector trigger (the collector moves only service principal requests) already covers this kind, and answers first.

-- 5. Registration evidence that waits for a valid agent.yaml (decided while planning: item 30).
ALTER TABLE registration_evidence DROP CONSTRAINT registration_evidence_problem_check;
ALTER TABLE registration_evidence ADD CONSTRAINT registration_evidence_problem_check
  CHECK (problem IN ('rules_missing', 'rules_invalid', 'rules_unreadable', 'repo_unreadable', 'record_unreadable', 'record_case_mismatch',
                     'agent_yaml_missing', 'agent_yaml_invalid'));

-- 6. The agent repos the collector saw (decided while planning: item 29): each one's agent.yaml document, or why it can't be used.
-- The collector replaces them all in one go; the web reads them (the web never calls GitHub). An agent.yaml is the repo's own file,
-- not a draft, so the web reads the table itself (no view).
CREATE TABLE agent_observed (
  repo text PRIMARY KEY CHECK (repo ~ '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'),
  commit_sha text NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  agent jsonb CHECK (agent IS NULL OR agent_v1_shape(agent)),  -- the definition's shape (section 3)
  problem text CHECK (problem IN ('agent_yaml_missing', 'agent_yaml_invalid')),
  observed_at timestamptz NOT NULL,
  CHECK ((agent IS NULL) <> (problem IS NULL))
);
CREATE UNIQUE INDEX agent_observed_repo ON agent_observed (lower(repo));

-- 7. Who does what. The web asks for an agent definition (an INSERT it already may) and reads the observed agents; the writer counts
-- a retried failure; the collector replaces the observed agents.
GRANT SELECT ON agent_observed TO portal_web;
GRANT SELECT, INSERT, DELETE ON agent_observed TO portal_collector;
GRANT UPDATE (attempts, last_error) ON request TO portal_writer;
