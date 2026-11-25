-- V2-P2 (Q4/Q15). Append-only narrow observation read model for Iris.
-- Domain commits are the single source of truth and this table is never
-- written inside a domain transaction and never rolls one back.
CREATE TABLE strategy_events (
    event_id text PRIMARY KEY,
    run_id text NOT NULL DEFAULT '',
    account_id text NOT NULL,
    subject_type text NOT NULL,
    subject_id text NOT NULL,
    status text NOT NULL,
    from_status text NOT NULL DEFAULT '',
    to_status text NOT NULL DEFAULT '',
    reason_code text NOT NULL DEFAULT '',
    symbol text NOT NULL DEFAULT '',
    edge_id text NOT NULL DEFAULT '',
    graph_version text NOT NULL DEFAULT '',
    occurred_at timestamptz NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX strategy_events_occurred ON strategy_events (occurred_at, event_id);
CREATE INDEX strategy_events_account ON strategy_events (account_id, occurred_at DESC);
CREATE INDEX strategy_events_subject ON strategy_events (subject_type, subject_id);
