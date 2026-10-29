-- 0006: binding requests (spec 2026-10-29 app bindings, B1). The app's admin links an Azure identity or a Databricks connection the
-- owning teams' processes already created, so there is no decision in the portal: a binding request starts approved, with no
-- decider, and its record is its draft. Only that kind may start approved; a registration still starts pending, and
-- request_no_self_decision is unchanged.
ALTER TABLE request DROP CONSTRAINT request_kind_check;
ALTER TABLE request ADD CONSTRAINT request_kind_check CHECK (kind IN ('registration', 'binding'));

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
       OR NEW.final->>'schema' IS DISTINCT FROM 'binding/v1' OR NEW.final->>'request' IS DISTINCT FROM NEW.id
       OR NEW.final->>'linked_by' IS DISTINCT FROM NEW.requested_by OR NEW.final->>'repo' IS DISTINCT FROM NEW.repo THEN
      RAISE EXCEPTION 'a binding request starts approved, its record is its draft, and it names its request, person and repo';
    END IF;
  ELSIF NEW.state <> 'pending' OR NEW.final IS NOT NULL THEN
    RAISE EXCEPTION 'a request starts pending with no decision or result';
  END IF;
  RETURN NEW;
END
$$;

-- Nobody approves a binding, so before it writes one the writer checks that the requester still administers the repo as the
-- collector last saw it. The writer can't read identity_link or repo_admin: it gets one person's observed admin repos through this
-- function only (the rows of repo_admin that person's active links match: by user ID where the link has one, by login otherwise).
CREATE FUNCTION person_admin_repos(p_person text)
RETURNS TABLE (repo text, github_login text, github_user_id bigint, observed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT DISTINCT a.repo, a.github_login, a.github_user_id, a.observed_at
  FROM identity_link l JOIN repo_admin a
    ON (l.github_user_id IS NOT NULL AND a.github_user_id = l.github_user_id)
    OR (l.github_user_id IS NULL AND lower(a.github_login) = lower(l.github_login))
  WHERE l.person_id = p_person AND l.removed_at IS NULL
$$;
REVOKE ALL ON FUNCTION person_admin_repos(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION person_admin_repos(text) TO portal_writer;
