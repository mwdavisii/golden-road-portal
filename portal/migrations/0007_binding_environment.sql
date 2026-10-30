-- 0007: bindings are per environment (spec 2026-10-29 GitOps deploy repo, G3). One open request per kind, repo and environment.
-- The environment lives in the binding document (binding/v2.environment), so there is no new column. A binding request recorded
-- before this migration (binding/v1, no environment) counts as test's, the environment builder 0.3.16's one binding was for; a
-- registration has no environment. Existing rows are not changed: a v1 binding request still open is failed by the writer's
-- recheck (it writes binding/v2 only), and a merged one is the test environment's current binding on the app's page.
DROP INDEX request_one_open;
CREATE UNIQUE INDEX request_one_open ON request (kind, lower(repo), (CASE WHEN kind = 'binding' THEN coalesce(draft->>'environment', 'test') ELSE '' END))
  WHERE state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open');

-- 0006's insert guard, with binding/v1 replaced by binding/v2 and the environment held to test or prod; nothing else changes.
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
  RETURN NEW;
END
$$;
