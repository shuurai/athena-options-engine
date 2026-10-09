CREATE TABLE settings_versions (
    id integer PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    author text NOT NULL,
    note text NOT NULL,
    payload jsonb NOT NULL,
    active boolean NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX settings_versions_one_active
    ON settings_versions (active)
    WHERE active;
