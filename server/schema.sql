-- ============================================================
--  PrintHub schema (MySQL)
--  Run:  mysql -u root -p < schema.sql
--  Then create an app user (mirrors the prototype's deploy_setup.sh).
-- ============================================================

CREATE DATABASE IF NOT EXISTS printhub
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE printhub;

-- ------------------------------------------------------------
-- Vendors (print shops / service centres) — spec §4, §6, §7
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendors (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    name            VARCHAR(255) NOT NULL,          -- owner / contact name
    shop_name       VARCHAR(255) NOT NULL,
    phone           VARCHAR(32),
    email           VARCHAR(255),
    shop_code       VARCHAR(32)  NOT NULL,          -- public URL slug (/shop/<code>)

    -- Auto-generated credentials, delivered after first payment (spec §4, §6.4)
    login_id        VARCHAR(32),
    password_hash   VARCHAR(255),
    worker_token    VARCHAR(64),                    -- desktop worker API token

    -- Subscription (spec §5, §6, §8.3)
    plan            VARCHAR(16)  NOT NULL DEFAULT 'monthly',  -- monthly|yearly|lifetime
    status          VARCHAR(24)  NOT NULL DEFAULT 'pending_payment',
        -- pending_payment | active | grace | suspended | rejected
    subscribed_at   DATETIME,
    renews_at       DATETIME,                       -- NULL for lifetime
    grace_until     DATETIME,

    -- Vendor pricing control (spec §6.5) — per page, set by the vendor
    price_bw        DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    price_colour    DECIMAL(10,2) NOT NULL DEFAULT 0.00,

    -- Printer configuration (spec §7) — printer/tray NAMES as reported by the
    -- vendor's worker PC; the worker resolves tray names to driver bin ids.
    printer_mode    VARCHAR(16)  NOT NULL DEFAULT 'single',   -- single|multi
    printer_single  VARCHAR(255),
    tray_single     VARCHAR(128),
    printer_bw      VARCHAR(255),
    tray_bw         VARCHAR(128),
    printer_colour  VARCHAR(255),
    tray_colour     VARCHAR(128),

    -- Per-vendor Cashfree account — EVERY SITE HAS A DIFFERENT ACCOUNT.
    -- Customer print-job payments for this shop go through these keys.
    cashfree_app_id         VARCHAR(128),
    cashfree_secret_key     VARCHAR(128),
    cashfree_webhook_secret VARCHAR(128),
    cashfree_env            VARCHAR(16) NOT NULL DEFAULT 'production',  -- production|sandbox

    created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_shop_code (shop_code),
    UNIQUE KEY uq_login_id (login_id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Payment log (spec §8.2) — installs, subscriptions, renewals
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    vendor_id   BIGINT        NOT NULL,
    kind        VARCHAR(24)   NOT NULL,   -- installation|subscription|renewal
    plan        VARCHAR(16),
    amount      DECIMAL(10,2) NOT NULL,
    status      VARCHAR(16)   NOT NULL DEFAULT 'paid',  -- paid|failed|pending
    method      VARCHAR(32)   DEFAULT 'manual',         -- manual|autopay|gateway
    reference   VARCHAR(128),                           -- gateway txn id etc.
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_vendor (vendor_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Activity log (spec §8.1) — logins, jobs, config changes, ...
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activity_log (
    id          BIGINT       NOT NULL AUTO_INCREMENT,
    vendor_id   BIGINT,                       -- NULL for platform-level events
    actor       VARCHAR(64)  NOT NULL,        -- vendor|worker|customer|admin|system
    action      VARCHAR(64)  NOT NULL,        -- login, job_created, printed, ...
    detail      TEXT,
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_vendor (vendor_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Print jobs — ported from the prototype, now vendor-scoped
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS print_jobs (
    id                 BIGINT       NOT NULL AUTO_INCREMENT,
    vendor_id          BIGINT       NOT NULL,
    customer_id        VARCHAR(255),
    customer_name      VARCHAR(255),
    doc_format         VARCHAR(24),          -- aadhaar|pan|voter|document
    file_url           TEXT,
    storage_key        VARCHAR(512),
    original_filename  VARCHAR(512),
    status             VARCHAR(32)  NOT NULL DEFAULT 'awaiting_payment',
        -- awaiting_payment|confirmed|printing|printed|cancelled
    total_pages        INT          DEFAULT 1,
    sides              VARCHAR(16)  DEFAULT 'single',
    orientation        VARCHAR(16)  DEFAULT 'portrait',
    color_mode         VARCHAR(16)  DEFAULT 'bw',
    paper_size         VARCHAR(16)  DEFAULT 'A4',
    price              DECIMAL(10,2) DEFAULT 0.00,
    payment_status     VARCHAR(32)  NOT NULL DEFAULT 'pending',   -- pending|paid|failed
    copies             INT          DEFAULT 1,
    order_id           VARCHAR(128),         -- Cashfree order (online payments)
    transaction_id     VARCHAR(128),
    paid_at            DATETIME,
    claimed_at         DATETIME,             -- when a worker took the job
    created_at         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_vendor_status (vendor_id, status),
    KEY idx_order (order_id),
    KEY idx_storage (storage_key(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------
-- Gateway orders — one row per Cashfree order (both the platform
-- account and each vendor's own account); drives webhook dispatch.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gateway_orders (
    id             BIGINT        NOT NULL AUTO_INCREMENT,
    order_id       VARCHAR(128)  NOT NULL,
    vendor_id      BIGINT,
    purpose        VARCHAR(24)   NOT NULL,   -- first_payment|renewal|print_job
    amount         DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    status         VARCHAR(16)   NOT NULL DEFAULT 'pending',  -- pending|paid|failed
    transaction_id VARCHAR(128),
    meta           TEXT,          -- one-time payload (e.g. generated creds)
    created_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_order (order_id),
    KEY idx_vendor (vendor_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Migration for a deployment created before the Cashfree integration:
--   ALTER TABLE vendors
--     ADD COLUMN cashfree_app_id VARCHAR(128),
--     ADD COLUMN cashfree_secret_key VARCHAR(128),
--     ADD COLUMN cashfree_webhook_secret VARCHAR(128),
--     ADD COLUMN cashfree_env VARCHAR(16) NOT NULL DEFAULT 'production';
--   ALTER TABLE print_jobs
--     ADD COLUMN order_id VARCHAR(128), ADD COLUMN transaction_id VARCHAR(128),
--     ADD COLUMN paid_at DATETIME, ADD KEY idx_order (order_id);
