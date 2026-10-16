CREATE TABLE IF NOT EXISTS advisor_signals (
    account_id text NOT NULL,
    symbol text NOT NULL,
    day date NOT NULL,
    action text NOT NULL,
    confidence double precision NOT NULL,
    reason text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol, day)
);

CREATE INDEX IF NOT EXISTS advisor_signals_day ON advisor_signals (day DESC);
