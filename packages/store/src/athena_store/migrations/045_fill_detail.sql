ALTER TABLE fills ADD COLUMN side text NOT NULL DEFAULT '';
ALTER TABLE fills ADD COLUMN sec_type text NOT NULL DEFAULT 'STK';
ALTER TABLE fills ADD COLUMN commission numeric;
ALTER TABLE fills ADD COLUMN commission_currency text NOT NULL DEFAULT '';
