CREATE TABLE account_marks (
    account_id text NOT NULL,
    day date NOT NULL,
    cash numeric,
    margin numeric,
    PRIMARY KEY (account_id, day)
);

ALTER TABLE fills ALTER COLUMN draft_id DROP NOT NULL;
ALTER TABLE fills ADD COLUMN exec_id text NOT NULL DEFAULT '';
CREATE UNIQUE INDEX fills_exec_id ON fills (account_id, exec_id) WHERE exec_id <> '';
