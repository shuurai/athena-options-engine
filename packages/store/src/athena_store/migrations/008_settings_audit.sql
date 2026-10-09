CREATE TABLE settings_audits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id text NOT NULL,
    settings_id integer NOT NULL,
    author text NOT NULL,
    note text NOT NULL,
    changes text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
