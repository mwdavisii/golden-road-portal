-- 0003: the writer records the commit it opened a pull request with (head_sha) and the base commit it branched from (base_sha), and
-- merges only that commit. And no admin links a GitHub login to their own person. (0001 and 0002 stay as they were applied.)

ALTER TABLE request ADD COLUMN head_sha text CHECK (head_sha ~ '^[0-9a-f]{40}$');
ALTER TABLE request ADD COLUMN base_sha text CHECK (base_sha ~ '^[0-9a-f]{40}$');
ALTER TABLE identity_link ADD CONSTRAINT identity_link_no_self_admin_link CHECK (source <> 'admin' OR created_by <> person_id);

GRANT UPDATE (head_sha, base_sha) ON request TO portal_writer;

CREATE OR REPLACE FUNCTION request_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL OR NEW.final IS NOT NULL OR NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL
     OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL OR NEW.failure_reason IS NOT NULL
     OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending with no decision or result';
  END IF;
  RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION request_update_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
