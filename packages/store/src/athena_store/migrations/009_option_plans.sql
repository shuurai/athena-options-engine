CREATE TABLE option_plans (
    symbol text PRIMARY KEY,
    expiry date NOT NULL,
    put_strike numeric NOT NULL,
    call_strike numeric NOT NULL,
    put_limit numeric,
    call_limit numeric,
    trading_class text NOT NULL,
    option_exchange text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE drafts ADD COLUMN expiry date;
ALTER TABLE drafts ADD COLUMN put_strike numeric;
ALTER TABLE drafts ADD COLUMN call_strike numeric;
ALTER TABLE drafts ADD COLUMN put_limit numeric;
ALTER TABLE drafts ADD COLUMN call_limit numeric;
ALTER TABLE drafts ADD COLUMN trading_class text NOT NULL DEFAULT '';
ALTER TABLE drafts ADD COLUMN option_exchange text NOT NULL DEFAULT '';

ALTER TABLE orders ADD COLUMN expiry date;
ALTER TABLE orders ADD COLUMN strike numeric;
ALTER TABLE orders ADD COLUMN contract_right text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN trading_class text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN option_exchange text NOT NULL DEFAULT '';
