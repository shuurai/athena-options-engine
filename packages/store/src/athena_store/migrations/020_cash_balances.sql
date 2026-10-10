ALTER TABLE account_marks ADD COLUMN base_currency text NOT NULL DEFAULT '';
ALTER TABLE account_marks ADD COLUMN cash_balances jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE account_marks ADD COLUMN fx_rates jsonb NOT NULL DEFAULT '{}'::jsonb;
