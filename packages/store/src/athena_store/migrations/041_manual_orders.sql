ALTER TABLE orders ADD COLUMN order_type text NOT NULL DEFAULT 'LMT';
ALTER TABLE orders ADD COLUMN stop_price numeric;
