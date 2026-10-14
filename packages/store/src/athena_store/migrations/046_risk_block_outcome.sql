ALTER TABLE decision_trails DROP CONSTRAINT IF EXISTS decision_trails_outcome_check;
ALTER TABLE decision_trails
    ADD CONSTRAINT decision_trails_outcome_check
    CHECK (outcome IN ('drafted', 'quiet', 'dropped', 'no_data', 'invalid', 'observed', 'blocked'));
