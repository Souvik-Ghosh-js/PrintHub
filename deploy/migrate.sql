-- ============================================================
--  PrintHub — idempotent column migrations.
--  schema.sql only creates missing TABLES; existing tables never
--  gain new columns from it. Every column added after a deployment
--  went live belongs here.
--
--  Safe to run repeatedly: each ALTER is skipped when the column
--  is already present.
-- ============================================================
USE printhub;

DROP PROCEDURE IF EXISTS ph_add_column;
DELIMITER $$
CREATE PROCEDURE ph_add_column(
    IN tbl VARCHAR(64), IN col VARCHAR(64), IN defn VARCHAR(255))
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = tbl AND COLUMN_NAME = col
    ) THEN
        SET @s = CONCAT('ALTER TABLE `', tbl, '` ADD COLUMN `', col, '` ', defn);
        PREPARE stmt FROM @s;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
        SELECT CONCAT('added ', tbl, '.', col) AS migration;
    END IF;
END$$
DELIMITER ;

-- Autopay mandate (Cashfree Subscriptions)
CALL ph_add_column('vendors', 'autopay_subscription_id', 'VARCHAR(128)');
CALL ph_add_column('vendors', 'autopay_status',          'VARCHAR(32)');
CALL ph_add_column('vendors', 'autopay_next_charge',     'VARCHAR(64)');

-- Per-vendor Cashfree account (for deployments predating it)
CALL ph_add_column('vendors', 'cashfree_app_id',         'VARCHAR(128)');
CALL ph_add_column('vendors', 'cashfree_secret_key',     'VARCHAR(128)');
CALL ph_add_column('vendors', 'cashfree_webhook_secret', 'VARCHAR(128)');
CALL ph_add_column('vendors', 'cashfree_env',            "VARCHAR(16) NOT NULL DEFAULT 'production'");

-- Online payment tracking on print jobs
CALL ph_add_column('print_jobs', 'order_id',       'VARCHAR(128)');
CALL ph_add_column('print_jobs', 'transaction_id', 'VARCHAR(128)');
CALL ph_add_column('print_jobs', 'paid_at',        'DATETIME');
CALL ph_add_column('print_jobs', 'claimed_at',     'DATETIME');
CALL ph_add_column('gateway_orders', 'meta',       'TEXT');

DROP PROCEDURE IF EXISTS ph_add_column;
