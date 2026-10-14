ALTER TABLE orders ADD COLUMN perm_id bigint;

CREATE UNIQUE INDEX orders_account_perm
    ON orders (account_id, perm_id)
    WHERE perm_id IS NOT NULL AND perm_id <> 0;
