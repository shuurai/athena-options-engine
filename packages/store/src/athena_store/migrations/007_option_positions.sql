ALTER TABLE positions ADD COLUMN sec_type text NOT NULL DEFAULT 'STK';
ALTER TABLE positions ADD COLUMN expiry date;
ALTER TABLE positions ADD COLUMN contract_right text NOT NULL DEFAULT '';
ALTER TABLE positions ADD COLUMN strike numeric;
ALTER TABLE positions ADD COLUMN market_price numeric;
