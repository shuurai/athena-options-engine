CREATE TABLE symbol_facts (
    symbol text PRIMARY KEY,
    name text NOT NULL DEFAULT '',
    market_cap numeric,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE symbol_phases ADD COLUMN classifiers text NOT NULL DEFAULT '';

ALTER TABLE symbol_phases ADD COLUMN thesis text NOT NULL DEFAULT '';
