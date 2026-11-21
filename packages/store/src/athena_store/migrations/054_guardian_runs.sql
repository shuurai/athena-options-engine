-- Guardian R3 scheduled-run grade store (booklet 14, section 14).
-- Append-only: one row per (account, trading day, run). Terminal rows are
-- never rewritten and a re-fire in the same slot is swallowed by the unique key.
-- NB: keep comment lines free of statement terminators (migrate splits on them).
-- Deliberately NOT guardian_alerts: that table is an ack state machine with a
-- partial unique index on live states (booklet 14, section 13.5 / D62).
CREATE TABLE IF NOT EXISTS guardian_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    trade_day date NOT NULL,
    run text NOT NULL,
    scheduled_at timestamptz NOT NULL,
    ran_at timestamptz NOT NULL DEFAULT now(),
    portfolio_tier text NOT NULL,
    account_tier text NOT NULL,
    breach_count integer NOT NULL DEFAULT 0,
    proximity_count integer NOT NULL DEFAULT 0,
    unmanaged_count integer NOT NULL DEFAULT 0,
    el_nl_pct numeric,
    maint_nl_pct numeric,
    account_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
    positions jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'ok',
    detail text NOT NULL DEFAULT '',
    UNIQUE (account_id, trade_day, run)
);

CREATE INDEX IF NOT EXISTS guardian_runs_latest
    ON guardian_runs (account_id, ran_at DESC, id DESC);
