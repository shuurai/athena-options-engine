CREATE TABLE chain_marks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol text NOT NULL,
    ts timestamptz NOT NULL DEFAULT now(),
    expiry date NOT NULL,
    dte integer NOT NULL,
    k_call numeric NOT NULL,
    k_put numeric NOT NULL,
    moneyness_pct numeric NOT NULL,
    mid_call numeric NOT NULL,
    mid_put numeric NOT NULL
);
