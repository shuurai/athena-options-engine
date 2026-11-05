CREATE TABLE order_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    account_id text NOT NULL,
    ib_status text NOT NULL DEFAULT '',
    status text NOT NULL,
    filled_qty numeric,
    remaining_qty numeric,
    avg_fill numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX order_events_order ON order_events (order_id, id);
