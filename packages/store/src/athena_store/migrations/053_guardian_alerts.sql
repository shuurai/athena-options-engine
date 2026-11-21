-- Guardian alert store (booklet 14, iteration R2).
-- One live row per (account, symbol, breach). Terminal rows are history, never deleted (D62).
CREATE TABLE IF NOT EXISTS guardian_alerts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    symbol text NOT NULL,
    breach text NOT NULL,
    tier text NOT NULL,
    state text NOT NULL DEFAULT 'open',
    source text NOT NULL DEFAULT 'grade',
    reason text NOT NULL DEFAULT '',
    ack_actor text NOT NULL DEFAULT '',
    ack_note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    skip_until timestamptz,
    resolved_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS guardian_alerts_live
    ON guardian_alerts (account_id, symbol, breach)
    WHERE state IN ('open', 'skipped');

CREATE INDEX IF NOT EXISTS guardian_alerts_open
    ON guardian_alerts (account_id, state, tier, created_at DESC);
