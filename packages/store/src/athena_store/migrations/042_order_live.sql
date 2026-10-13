ALTER TABLE orders ADD COLUMN ib_status text NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN filled_qty numeric;
ALTER TABLE orders ADD COLUMN remaining_qty numeric;
ALTER TABLE orders ADD COLUMN avg_fill numeric;
ALTER TABLE orders ADD COLUMN updated_at timestamptz;
