-- 0001: people, sessions, login states, identity links, requests, the append-only audit log, observed repo admins.
-- Run as portal_owner by `python -m portal.migrate`. The roles portal_web, portal_writer and portal_collector must exist (README).
CREATE TABLE person (
  id text PRIMARY KEY CHECK (id ~ '^p_[0-9a-f]{32}$'),
  issuer text NOT NULL,
  subject text NOT NULL,
  display_name text NOT NULL,
  email text,
  last_roles text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL,
  last_sign_in_at timestamptz NOT NULL,
  UNIQUE (issuer, subject)
);
CREATE TABLE session (
  token_hash bytea PRIMARY KEY,
  person_id text NOT NULL REFERENCES person (id),
  groups text[] NOT NULL,
  roles text[] NOT NULL,
  csrf text NOT NULL,
  created_at timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  expires_at timestamptz NOT NULL
);
CREATE TABLE login_state (
  state_hash bytea PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('oidc', 'github')),
  nonce text NOT NULL,
  code_verifier text NOT NULL,
  next_path text NOT NULL,
  person_id text REFERENCES person (id),
  expires_at timestamptz NOT NULL
);
CREATE TABLE identity_link (
  id text PRIMARY KEY CHECK (id ~ '^l_[0-9a-f]{32}$'),
  person_id text NOT NULL REFERENCES person (id),
  github_login text NOT NULL,
  github_user_id bigint,
  source text NOT NULL CHECK (source IN ('github-oauth', 'admin')),
  created_by text NOT NULL REFERENCES person (id),
  created_at timestamptz NOT NULL,
  removed_by text REFERENCES person (id),
  removed_at timestamptz
);
CREATE UNIQUE INDEX identity_link_login ON identity_link (lower(github_login)) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX identity_link_user ON identity_link (github_user_id) WHERE removed_at IS NULL AND github_user_id IS NOT NULL;
CREATE TABLE request (
  id text PRIMARY KEY CHECK (id ~ '^r_[0-9a-f]{32}$'),
  kind text NOT NULL CHECK (kind IN ('registration')),
  repo text NOT NULL,
  state text NOT NULL CHECK (state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open', 'merged', 'failed')),
  draft jsonb NOT NULL,
  requested_by text NOT NULL REFERENCES person (id),
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  decided_by text REFERENCES person (id),
  decided_at timestamptz,
  decision_groups text[],
  decision_note text,
  final jsonb,
  branch text,
  pr_number integer,
  pr_url text,
  merge_sha text,
  failure_reason text
);
CREATE UNIQUE INDEX request_one_open ON request (kind, lower(repo)) WHERE state IN ('pending', 'sent_back', 'approved', 'writing', 'pr_open');
CREATE TABLE audit_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at timestamptz NOT NULL,
  actor_kind text NOT NULL CHECK (actor_kind IN ('person', 'writer', 'system')),
  actor_id text,
  action text NOT NULL,
  subject_kind text NOT NULL,
  subject_id text NOT NULL,
  before jsonb,
  after jsonb,
  reason text NOT NULL CHECK (length(reason) > 0)
);
CREATE FUNCTION audit_log_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END
$$;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_log_is_append_only();
CREATE TABLE repo_admin (
  repo text NOT NULL,
  github_login text NOT NULL,
  github_user_id bigint NOT NULL,
  source text NOT NULL,
  observed_at timestamptz NOT NULL,
  PRIMARY KEY (repo, github_user_id)
);

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON person TO portal_web;
GRANT SELECT, INSERT, UPDATE, DELETE ON session TO portal_web;
GRANT SELECT, INSERT, DELETE ON login_state TO portal_web;
GRANT SELECT, INSERT, UPDATE ON identity_link TO portal_web;
GRANT SELECT, INSERT, UPDATE ON request TO portal_web;
GRANT SELECT ON repo_admin TO portal_web;
GRANT SELECT ON person TO portal_writer;
GRANT SELECT, UPDATE ON request TO portal_writer;
GRANT SELECT, INSERT ON audit_log TO portal_web, portal_writer;
GRANT SELECT, INSERT, DELETE ON repo_admin TO portal_collector;
