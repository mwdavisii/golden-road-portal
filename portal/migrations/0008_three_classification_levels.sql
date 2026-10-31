-- 0008: three classification levels (builder 0.5.0, spec 2026-10-30): public < confidential < protected replace public, internal,
-- restricted and phi. registration_evidence is insert-only, so the rows written before stay as they were: the new check is NOT VALID
-- and holds for every row inserted from now on. An approval against evidence that still names a retired level is refused: the admin
-- sends the request back, and the resubmission gets fresh evidence. (0001 to 0007 stay as they were applied.)
ALTER TABLE registration_evidence DROP CONSTRAINT registration_evidence_floor_classification_check;
ALTER TABLE registration_evidence ADD CONSTRAINT registration_evidence_floor_classification_check
  CHECK (floor_classification IN ('public', 'confidential', 'protected')) NOT VALID;

CREATE OR REPLACE FUNCTION request_approval_floor_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  ev record;
  tiers text[] := ARRAY['low', 'moderate', 'high'];
  classes text[] := ARRAY['public', 'confidential', 'protected'];
  below boolean;
BEGIN
  IF NOT (OLD.state = 'pending' AND NEW.state = 'approved') THEN RETURN NEW; END IF;
  SELECT commit_sha, floor_tier, floor_classification INTO ev FROM registration_evidence
   WHERE request_id = NEW.id AND submission = NEW.submission AND problem IS NULL;
  IF NOT FOUND THEN RAISE EXCEPTION 'an approval needs the platform''s evidence for this submission'; END IF;
  IF array_position(classes, ev.floor_classification) IS NULL THEN
    RAISE EXCEPTION 'the evidence names a retired classification level: send the request back so the platform computes it again';
  END IF;
  IF NEW.final->>'evidence_commit' IS DISTINCT FROM ev.commit_sha OR NEW.final->>'floor_tier' IS DISTINCT FROM ev.floor_tier
     OR NEW.final->>'floor_classification' IS DISTINCT FROM ev.floor_classification THEN
    RAISE EXCEPTION 'the approved record carries the stored evidence commit and floor';
  END IF;
  IF NEW.final->'floor_override' IS DISTINCT FROM NEW.floor_override THEN
    RAISE EXCEPTION 'the record and the request carry the same override';
  END IF;
  below := coalesce(array_position(tiers, NEW.final->>'tier'), 0) < array_position(tiers, ev.floor_tier)
        OR coalesce(array_position(classes, NEW.final->>'data_classification'), 0) < array_position(classes, ev.floor_classification);
  IF below THEN
    IF NEW.floor_override IS NULL OR NEW.floor_override->>'by' IS DISTINCT FROM NEW.decided_by
       OR length(coalesce(NEW.floor_override->>'reason', '')) < 30 THEN
      RAISE EXCEPTION 'an approval below the floor needs the approver''s override reason of at least 30 characters';
    END IF;
    IF NEW.floor_override->>'at' IS DISTINCT FROM NEW.final->>'approved_at' OR length(NEW.floor_override->>'reason') > 1000
       OR NEW.floor_override->>'reason' ~ '[\r\n]' THEN
      RAISE EXCEPTION 'the override carries the approval time and a single line of at most 1000 characters';
    END IF;
  ELSIF NEW.floor_override IS NOT NULL THEN
    RAISE EXCEPTION 'an override is recorded only for an approval below the floor';
  END IF;
  RETURN NEW;
END
$$;
