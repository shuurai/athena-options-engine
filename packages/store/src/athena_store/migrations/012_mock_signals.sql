CREATE TABLE mock_signals (
    symbol text PRIMARY KEY,
    tags text NOT NULL,
    event_offset integer,
    send_order boolean NOT NULL DEFAULT false,
    active boolean NOT NULL DEFAULT true,
    note text NOT NULL DEFAULT ''
);
