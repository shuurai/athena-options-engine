DELETE FROM market_bars WHERE source = 'seed';
DELETE FROM market_bars WHERE source = 'pool-sync';

ALTER TABLE symbol_facts ADD COLUMN history_short boolean NOT NULL DEFAULT false;
