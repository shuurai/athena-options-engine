ALTER TABLE ai_calls ADD COLUMN simulated boolean NOT NULL DEFAULT false;
ALTER TABLE decision_trails ADD COLUMN simulated boolean NOT NULL DEFAULT false;
