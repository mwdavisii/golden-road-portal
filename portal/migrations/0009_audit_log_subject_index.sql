-- 0009: the request page's timeline reads one subject's audit entries (store.audit_for), oldest first. (0001 to 0008 stay as they
-- were applied.)
CREATE INDEX audit_log_subject ON audit_log (subject_kind, subject_id, id);
