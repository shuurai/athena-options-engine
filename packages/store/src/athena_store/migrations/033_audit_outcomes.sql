ALTER TABLE decision_trails
    ADD CONSTRAINT decision_trails_outcome_check
    CHECK (outcome IN ('drafted', 'quiet', 'dropped', 'no_data', 'invalid', 'observed'));

ALTER TABLE ai_calls
    ADD CONSTRAINT ai_calls_status_check
    CHECK (status IN ('ok', 'error', 'invalid'));
