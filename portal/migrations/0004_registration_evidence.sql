-- 0004: the platform's evidence for a registration (spec 2026-10-23-registration-evidence-design.md). Insert-only; the collector may
-- insert it and read which requests wait (never their drafts). A resubmission counts up, so it gets fresh evidence. The approval rule
-- that uses the evidence is 0005. (0001 to 0003 stay as they were applied.)
ALTER TABLE request ADD COLUMN submission integer NOT NULL DEFAULT 1 CHECK (submission >= 1);
ALTER TABLE request ADD COLUMN floor_override jsonb;

CREATE TABLE registration_evidence (
  request_id text NOT NULL REFERENCES request (id),
  submission integer NOT NULL CHECK (submission >= 1),
  commit_sha text CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
  evidence jsonb,
  floor_tier text CHECK (floor_tier IN ('low', 'moderate', 'high')),
  floor_classification text CHECK (floor_classification IN ('public', 'internal', 'restricted', 'phi')),
  rules_commit text CHECK (rules_commit ~ '^[0-9a-f]{40}$'),
  problem text CHECK (problem IN ('rules_missing', 'rules_invalid', 'rules_unreadable', 'repo_unreadable')),
  computed_at timestamptz NOT NULL,
  CHECK ((problem IS NULL AND commit_sha IS NOT NULL AND evidence IS NOT NULL AND floor_tier IS NOT NULL AND floor_classification IS NOT NULL
          AND rules_commit IS NOT NULL)
      OR (problem IS NOT NULL AND commit_sha IS NULL AND evidence IS NULL AND floor_tier IS NULL AND floor_classification IS NULL
          AND rules_commit IS NULL))
);
CREATE UNIQUE INDEX registration_evidence_once ON registration_evidence (request_id, submission) WHERE problem IS NULL;
CREATE UNIQUE INDEX registration_evidence_problem_once ON registration_evidence (request_id, submission, problem) WHERE problem IS NOT NULL;

CREATE FUNCTION registration_evidence_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  SELECT kind, repo, state, submission INTO r FROM request WHERE id = NEW.request_id;
  IF NOT FOUND OR r.kind <> 'registration' OR r.state <> 'pending' OR r.submission <> NEW.submission THEN
    RAISE EXCEPTION 'evidence is recorded only for a pending registration''s current submission';
  END IF;
  IF NEW.problem IS NULL AND NEW.evidence IS NOT NULL AND (NEW.evidence->>'repo' IS DISTINCT FROM r.repo OR NEW.evidence->>'commit' IS DISTINCT FROM NEW.commit_sha) THEN
    RAISE EXCEPTION 'evidence names its request''s repo and its commit';
  END IF;
  NEW.computed_at := now();
  RETURN NEW;
END
$$;
CREATE TRIGGER registration_evidence_insert BEFORE INSERT ON registration_evidence FOR EACH ROW EXECUTE FUNCTION registration_evidence_insert_guard();

CREATE FUNCTION registration_evidence_is_insert_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'registration_evidence is insert-only';
END
$$;
CREATE TRIGGER registration_evidence_no_change BEFORE UPDATE OR DELETE ON registration_evidence FOR EACH ROW EXECUTE FUNCTION registration_evidence_is_insert_only();
CREATE TRIGGER registration_evidence_no_truncate BEFORE TRUNCATE ON registration_evidence FOR EACH STATEMENT EXECUTE FUNCTION registration_evidence_is_insert_only();

GRANT SELECT, INSERT ON registration_evidence TO portal_collector;
GRANT SELECT (id, kind, repo, state, submission) ON request TO portal_collector;
GRANT SELECT ON registration_evidence TO portal_web, portal_writer;
GRANT UPDATE (submission, floor_override) ON request TO portal_web;

CREATE OR REPLACE FUNCTION request_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state <> 'pending' OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL OR NEW.decision_groups IS NOT NULL
     OR NEW.decision_note IS NOT NULL OR NEW.final IS NOT NULL OR NEW.branch IS NOT NULL OR NEW.pr_number IS NOT NULL
     OR NEW.pr_url IS NOT NULL OR NEW.merge_sha IS NOT NULL OR NEW.failure_reason IS NOT NULL
     OR NEW.head_sha IS NOT NULL OR NEW.base_sha IS NOT NULL OR NEW.submission <> 1 OR NEW.floor_override IS NOT NULL THEN
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
