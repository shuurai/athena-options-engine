CREATE TABLE tickets (
    id           text PRIMARY KEY,
    account_id   text NOT NULL,
    symbol       text NOT NULL,
    state_before text NOT NULL,
    state_after  text NOT NULL,
    legs         integer NOT NULL,
    reason       text NOT NULL,
    status       text NOT NULL,
    simulated    boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE drafts ADD COLUMN ticket_id text;
ALTER TABLE drafts ADD COLUMN leg_index integer;
CREATE INDEX tickets_symbol_idx ON tickets (account_id, symbol, simulated, created_at DESC);
