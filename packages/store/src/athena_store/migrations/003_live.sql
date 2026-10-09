ALTER TABLE drafts ADD COLUMN limit_price numeric;
ALTER TABLE drafts ADD COLUMN instrument text;

CREATE TABLE orders (
    id text PRIMARY KEY,
    draft_id text NOT NULL REFERENCES drafts (id),
    account_id text NOT NULL,
    symbol text NOT NULL,
    tactic text NOT NULL,
    action text NOT NULL,
    contracts integer NOT NULL,
    limit_price numeric,
    status text NOT NULL,
    ib_order_id bigint,
    block_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE positions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    symbol text NOT NULL,
    quantity numeric NOT NULL,
    avg_price numeric NOT NULL,
    snapshot_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE halts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reason text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    cleared_at timestamptz
);

CREATE UNIQUE INDEX halts_one_active ON halts (active) WHERE active;

CREATE TABLE nav_marks (
    account_id text NOT NULL,
    day date NOT NULL,
    start_nav numeric NOT NULL,
    last_nav numeric NOT NULL,
    PRIMARY KEY (account_id, day)
);
