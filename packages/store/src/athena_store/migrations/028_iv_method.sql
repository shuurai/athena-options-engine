ALTER TABLE iv_marks ADD COLUMN method text NOT NULL DEFAULT 'premium_ratio';
ALTER TABLE iv_marks ADD COLUMN moneyness_pct numeric;
