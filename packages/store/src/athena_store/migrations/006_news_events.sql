CREATE TABLE news_events (
    fingerprint text PRIMARY KEY,
    ts timestamptz NOT NULL,
    symbol text NOT NULL,
    source text NOT NULL,
    event_type text NOT NULL,
    sentiment numeric NOT NULL,
    title text NOT NULL,
    url text NOT NULL DEFAULT ''
);

CREATE INDEX news_events_symbol_ts ON news_events (symbol, ts DESC);

CREATE TABLE calendar_marks (
    kind text NOT NULL,
    symbol text NOT NULL,
    event_date date NOT NULL,
    title text NOT NULL,
    PRIMARY KEY (kind, symbol, event_date)
);
