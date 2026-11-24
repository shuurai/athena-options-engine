-- R7 midday regime label store (booklet 14, section 13.5 / D69).
-- One row per (account, trading day, run): the unique key keeps it a single
-- observation, while the writer updates in place so a later VIX sync or a manual
-- recompute can refresh the reading. created_at therefore holds the first
-- sighting and is never rewritten. This differs from guardian_runs on purpose -
-- a grade is a frozen audit record, a regime label is an observation.
-- NB: keep comment lines free of statement terminators (migrate splits on them).
-- Deliberately NOT guardian_alerts: that table is an ack state machine with a
-- partial unique index on live states (D62). A regime label is a time series,
-- not an alert to acknowledge.
CREATE TABLE IF NOT EXISTS regime_labels (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    trade_day date NOT NULL,
    run text NOT NULL DEFAULT 'C',
    label text NOT NULL,
    close_bias text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, trade_day, run)
);

CREATE INDEX IF NOT EXISTS regime_labels_latest
    ON regime_labels (account_id, created_at DESC, id DESC);
