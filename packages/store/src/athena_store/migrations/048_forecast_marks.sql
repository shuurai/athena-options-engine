CREATE TABLE IF NOT EXISTS forecast_marks (
    account_id text NOT NULL,
    symbol text NOT NULL,
    day date NOT NULL,
    as_of text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT 'forecast',
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, symbol, day)
);
