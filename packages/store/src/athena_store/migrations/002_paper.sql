CREATE TABLE market_bars (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol text NOT NULL,
    ts timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric NOT NULL,
    source text NOT NULL,
    UNIQUE (symbol, ts, source)
);

CREATE TABLE drafts (
    id text PRIMARY KEY,
    symbol text NOT NULL,
    tactic text NOT NULL,
    contracts integer NOT NULL,
    status text NOT NULL,
    reason text NOT NULL,
    phase text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draft_id text NOT NULL REFERENCES drafts (id),
    actor text NOT NULL,
    decision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fills (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draft_id text NOT NULL REFERENCES drafts (id),
    symbol text NOT NULL,
    tactic text NOT NULL,
    contracts integer NOT NULL,
    price numeric NOT NULL,
    simulated boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE control_links (
    token text PRIMARY KEY,
    draft_id text NOT NULL REFERENCES drafts (id),
    expires_at timestamptz NOT NULL,
    used_at timestamptz
);

CREATE TABLE symbol_phases (
    symbol text PRIMARY KEY,
    phase text NOT NULL,
    tags text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
