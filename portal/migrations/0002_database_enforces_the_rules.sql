-- 0002: the database enforces the request state machine, no self-approval, and a tamper-proof audit log, so a bug or a
-- stolen web or writer credential can't do what the Python code refuses to do. (0001 is released with the first deploy;
-- this is a new migration so that the recorded hash of 0001 stays valid.)

-- No one decides their own registration, whichever process writes the row.
ALTER TABLE request ADD CONSTRAINT request_no_self_decision CHECK (decided_by IS NULL OR decided_by <> requested_by);

-- A request enters as a plain pending one: no decision, no final record, nothing from the writer.
CREATE FUNCTION request_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL OR NEW.final IS NOT NULL OR NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL
     OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL OR NEW.failure_reason IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending with no decision or result';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER request_insert BEFORE INSERT ON request FOR EACH ROW EXECUTE FUNCTION request_insert_guard();

-- Every update is one legal transition, made by the role that owns it, and changes only the columns that transition changes.
--   web:    pending -> approved | sent_back (the decision), sent_back -> pending (resubmitted)
--   writer: approved -> writing -> pr_open -> merged, writing | pr_open -> failed
-- The schema owner (migrations, a DBA) may make either kind; no one may skip a step.
CREATE FUNCTION request_update_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  pair text := OLD.state || '>' || NEW.state;
  web_pairs text[] := ARRAY['pending>approved', 'pending>sent_back', 'sent_back>pending'];
  writer_pairs text[] := ARRAY['approved>writing', 'writing>pr_open', 'pr_open>merged', 'writing>failed', 'pr_open>failed'];
BEGIN
  IF NEW.id IS DISTINCT FROM OLD.id OR NEW.kind IS DISTINCT FROM OLD.kind OR NEW.repo IS DISTINCT FROM OLD.repo
     OR NEW.requested_by IS DISTINCT FROM OLD.requested_by OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION 'a request''s id, kind, repo, requester and creation time never change';
  END IF;
  IF current_user = 'portal_web' THEN
    IF NOT pair = ANY (web_pairs) THEN RAISE EXCEPTION 'illegal request transition % for portal_web', pair; END IF;
  ELSIF current_user = 'portal_writer' THEN
    IF NOT pair = ANY (writer_pairs) THEN RAISE EXCEPTION 'illegal request transition % for portal_writer', pair; END IF;
  ELSIF NOT (pair = ANY (web_pairs) OR pair = ANY (writer_pairs)) THEN
    RAISE EXCEPTION 'illegal request transition %', pair;
  END IF;

  IF pair IN ('pending>approved', 'pending>sent_back') THEN
    IF NEW.draft IS DISTINCT FROM OLD.draft THEN RAISE EXCEPTION 'a decision does not change the draft'; END IF;
    IF NEW.decided_by IS NULL OR NEW.decided_at IS NULL OR NEW.decision_groups IS NULL OR coalesce(NEW.decision_note, '') = '' THEN
      RAISE EXCEPTION 'a decision records who, when, their groups and why';
    END IF;
    IF pair = 'pending>approved' AND NEW.final IS NULL THEN RAISE EXCEPTION 'an approval records the final record'; END IF;
    IF pair = 'pending>sent_back' AND NEW.final IS NOT NULL THEN RAISE EXCEPTION 'a request sent back has no final record'; END IF;
  ELSIF pair = 'sent_back>pending' THEN
    IF NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL OR NEW.decision_note IS NOT NULL
       OR NEW.final IS NOT NULL THEN
      RAISE EXCEPTION 'a resubmitted request starts without a decision';
    END IF;
  ELSE
    -- the writer's steps: the draft, the final record and the decision are frozen once approved
    IF NEW.draft IS DISTINCT FROM OLD.draft OR NEW.final IS DISTINCT FROM OLD.final OR NEW.decided_by IS DISTINCT FROM OLD.decided_by
       OR NEW.decided_at IS DISTINCT FROM OLD.decided_at OR NEW.decision_groups IS DISTINCT FROM OLD.decision_groups
       OR NEW.decision_note IS DISTINCT FROM OLD.decision_note THEN
      RAISE EXCEPTION 'the draft, the final record and the decision of an approved request never change';
    END IF;
    IF pair = 'writing>pr_open' AND (NEW.branch IS NULL OR NEW.pr_number IS NULL OR NEW.pr_url IS NULL) THEN
      RAISE EXCEPTION 'an open pull request records its branch, number and URL';
    END IF;
    IF pair = 'pr_open>merged' AND NEW.merge_sha IS NULL THEN RAISE EXCEPTION 'a merge records its commit'; END IF;
    IF NEW.state = 'failed' AND coalesce(NEW.failure_reason, '') = '' THEN RAISE EXCEPTION 'a failure records its reason'; END IF;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER request_update BEFORE UPDATE ON request FOR EACH ROW EXECUTE FUNCTION request_update_guard();

-- Column-level grants: each process may write only the columns of its own transitions.
REVOKE UPDATE ON request FROM portal_web, portal_writer;
GRANT UPDATE (state, draft, updated_at, decided_by, decided_at, decision_groups, decision_note, final) ON request TO portal_web;
GRANT UPDATE (state, updated_at, branch, pr_number, pr_url, merge_sha, failure_reason) ON request TO portal_writer;

-- The audit log's id and time are the database's, whatever an INSERT says: GENERATED ALWAYS alone can be bypassed with
-- OVERRIDING SYSTEM VALUE, so the trigger takes the id from the sequence and the time from the transaction.
CREATE FUNCTION audit_log_set_id_and_time() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  NEW.id := nextval(pg_get_serial_sequence('public.audit_log', 'id'));
  NEW.at := now();
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION audit_log_set_id_and_time() FROM PUBLIC;
CREATE TRIGGER audit_log_id_and_time BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_set_id_and_time();
