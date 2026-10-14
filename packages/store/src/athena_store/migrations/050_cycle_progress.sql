CREATE TABLE IF NOT EXISTS cycle_progress (
    id integer PRIMARY KEY CHECK (id = 1),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
