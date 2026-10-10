CREATE TABLE sim_runs (
    id text PRIMARY KEY,
    account_id text NOT NULL,
    symbols text NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    cursor_date date,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sim_runs_account ON sim_runs (account_id, created_at DESC);

CREATE TABLE sim_bars (
    run_id text NOT NULL REFERENCES sim_runs (id) ON DELETE CASCADE,
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric NOT NULL,
    PRIMARY KEY (run_id, symbol, ts)
);

ALTER TABLE positions ADD COLUMN simulated boolean NOT NULL DEFAULT false;
ALTER TABLE orders ADD COLUMN simulated boolean NOT NULL DEFAULT false;
ALTER TABLE drafts ADD COLUMN simulated boolean NOT NULL DEFAULT false;
