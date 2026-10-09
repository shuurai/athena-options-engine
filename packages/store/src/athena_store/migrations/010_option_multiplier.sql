ALTER TABLE option_plans ADD COLUMN multiplier text NOT NULL DEFAULT '100';
ALTER TABLE drafts ADD COLUMN multiplier text NOT NULL DEFAULT '100';
ALTER TABLE orders ADD COLUMN multiplier text NOT NULL DEFAULT '100';
