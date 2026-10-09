ALTER TABLE drafts ADD COLUMN account_id text NOT NULL DEFAULT '';
ALTER TABLE fills ADD COLUMN account_id text NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN account_id text NOT NULL DEFAULT '';
ALTER TABLE halts ADD COLUMN account_id text NOT NULL DEFAULT '';
ALTER TABLE settings_versions ADD COLUMN account_id text NOT NULL DEFAULT '';
ALTER TABLE symbol_phases ADD COLUMN account_id text NOT NULL DEFAULT '';

ALTER TABLE symbol_phases DROP CONSTRAINT symbol_phases_pkey;
ALTER TABLE symbol_phases ADD PRIMARY KEY (account_id, symbol);

DROP INDEX halts_one_active;
CREATE UNIQUE INDEX halts_one_active_per_account ON halts (account_id) WHERE active;

DROP INDEX settings_versions_one_active;
CREATE UNIQUE INDEX settings_versions_one_active_per_account
    ON settings_versions (account_id) WHERE active;
