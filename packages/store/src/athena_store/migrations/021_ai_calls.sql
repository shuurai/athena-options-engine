CREATE TABLE ai_calls (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    called_at timestamptz NOT NULL DEFAULT now(),
    agent text NOT NULL,
    symbol text NOT NULL,
    model text NOT NULL,
    provider text NOT NULL,
    thinking text NOT NULL,
    latency_ms integer NOT NULL,
    tokens integer NOT NULL,
    status text NOT NULL,
    result text NOT NULL,
    response jsonb NOT NULL
);

CREATE INDEX ai_calls_account_time ON ai_calls (account_id, called_at DESC);
