CREATE TABLE decision_trails (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    logged_at timestamptz NOT NULL DEFAULT now(),
    symbol text NOT NULL,
    source text NOT NULL,
    outcome text NOT NULL,
    summary text NOT NULL,
    steps jsonb NOT NULL
);

CREATE INDEX decision_trails_account_time ON decision_trails (account_id, logged_at DESC);
