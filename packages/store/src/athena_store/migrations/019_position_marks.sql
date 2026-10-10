ALTER TABLE positions ADD COLUMN currency text NOT NULL DEFAULT '';
ALTER TABLE positions ADD COLUMN multiplier numeric;
ALTER TABLE positions ADD COLUMN market_value numeric;
ALTER TABLE positions ADD COLUMN unrealized_pnl numeric;
ALTER TABLE positions ADD COLUMN realized_pnl numeric;
