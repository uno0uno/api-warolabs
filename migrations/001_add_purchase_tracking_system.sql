-- Migration: Add Purchase Tracking System with ACID compliance
-- Description: Adds tables and fields for complete purchase order traceability
-- Author: System
-- Date: 2025-11-13

BEGIN;

-- ============================================================================
-- 1. CREATE PURCHASE STATUS HISTORY TABLE
-- ============================================================================
-- Tracks all state transitions with full audit trail

CREATE TABLE IF NOT EXISTS purchase_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    from_status VARCHAR(50),
    to_status VARCHAR(50) NOT NULL,
    changed_by UUID NOT NULL,
    changed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Estado-specific metadata stored as JSONB
    metadata JSONB DEFAULT '{}'::jsonb,

    -- General notes for this transition
    notes TEXT,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Foreign key constraints with CASCADE for data integrity
    CONSTRAINT fk_purchase_status_purchase
        FOREIGN KEY (purchase_id)
        REFERENCES tenant_purchases(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_purchase_status_tenant
        FOREIGN KEY (tenant_id)
        REFERENCES tenants(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_purchase_status_user
        FOREIGN KEY (changed_by)
        REFERENCES profile(id)
        ON DELETE RESTRICT,

    -- Ensure valid state transitions
    CONSTRAINT chk_valid_status
        CHECK (to_status IN (
            'pending', 'confirmed', 'preparing', 'shipped',
            'partially_received', 'received', 'verified',
            'invoiced', 'paid', 'cancelled', 'overdue'
        ))
);

-- Indexes for performance
CREATE INDEX idx_purchase_status_history_purchase_id ON purchase_status_history(purchase_id);
CREATE INDEX idx_purchase_status_history_tenant_id ON purchase_status_history(tenant_id);
CREATE INDEX idx_purchase_status_history_to_status ON purchase_status_history(to_status);
CREATE INDEX idx_purchase_status_history_changed_at ON purchase_status_history(changed_at DESC);

COMMENT ON TABLE purchase_status_history IS 'Tracks all status changes for purchase orders with full audit trail';
COMMENT ON COLUMN purchase_status_history.metadata IS 'JSONB field for storing state-specific data (tracking numbers, confirmation details, etc.)';

-- ============================================================================
-- 2. CREATE PURCHASE ATTACHMENTS TABLE
-- ============================================================================
-- Stores file references from Cloudflare R2

CREATE TABLE IF NOT EXISTS purchase_attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_id UUID NOT NULL,
    tenant_id UUID NOT NULL,

    -- Cloudflare R2 path (full URL or path)
    path TEXT NOT NULL,

    -- File metadata
    file_name VARCHAR(255) NOT NULL,
    file_size BIGINT,
    mime_type VARCHAR(100),

    -- Attachment context
    attachment_type VARCHAR(50) NOT NULL,
    related_status VARCHAR(50),
    description TEXT,

    -- Audit fields
    uploaded_by UUID NOT NULL,
    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Foreign key constraints with CASCADE
    CONSTRAINT fk_purchase_attachment_purchase
        FOREIGN KEY (purchase_id)
        REFERENCES tenant_purchases(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_purchase_attachment_tenant
        FOREIGN KEY (tenant_id)
        REFERENCES tenants(id)
        ON DELETE CASCADE,

    CONSTRAINT fk_purchase_attachment_user
        FOREIGN KEY (uploaded_by)
        REFERENCES profile(id)
        ON DELETE RESTRICT,

    -- Ensure valid attachment types
    CONSTRAINT chk_valid_attachment_type
        CHECK (attachment_type IN (
            'purchase_order', 'confirmation', 'shipping_label',
            'invoice', 'payment_proof', 'quality_photo',
            'delivery_photo', 'other'
        )),

    -- Ensure path is not empty
    CONSTRAINT chk_path_not_empty
        CHECK (length(trim(path)) > 0)
);

-- Indexes for performance
CREATE INDEX idx_purchase_attachments_purchase_id ON purchase_attachments(purchase_id);
CREATE INDEX idx_purchase_attachments_tenant_id ON purchase_attachments(tenant_id);
CREATE INDEX idx_purchase_attachments_type ON purchase_attachments(attachment_type);
CREATE INDEX idx_purchase_attachments_status ON purchase_attachments(related_status);

COMMENT ON TABLE purchase_attachments IS 'Stores file attachments for purchase orders (invoices, shipping labels, photos) stored in Cloudflare R2';
COMMENT ON COLUMN purchase_attachments.path IS 'Full URL or path to file in Cloudflare R2 storage';

-- ============================================================================
-- 3. ADD TRACKING FIELDS TO TENANT_PURCHASES
-- ============================================================================

-- Add tracking and logistics fields
ALTER TABLE tenant_purchases
    ADD COLUMN IF NOT EXISTS confirmation_number VARCHAR(100),
    ADD COLUMN IF NOT EXISTS tracking_number VARCHAR(100),
    ADD COLUMN IF NOT EXISTS carrier VARCHAR(100),
    ADD COLUMN IF NOT EXISTS estimated_delivery_date TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS package_count INTEGER,

    -- State transition timestamps
    ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS preparing_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS shipped_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS invoiced_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP WITH TIME ZONE,

    -- Additional metadata
    ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50),
    ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(100),
    ADD COLUMN IF NOT EXISTS cancellation_reason TEXT,

    -- Received by tracking
    ADD COLUMN IF NOT EXISTS received_by UUID,
    ADD COLUMN IF NOT EXISTS verified_by UUID,

    -- Package condition
    ADD COLUMN IF NOT EXISTS package_condition VARCHAR(50);

-- Add foreign key constraints for new user references
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_purchase_received_by'
    ) THEN
        ALTER TABLE tenant_purchases
            ADD CONSTRAINT fk_purchase_received_by
                FOREIGN KEY (received_by)
                REFERENCES profile(id)
                ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_purchase_verified_by'
    ) THEN
        ALTER TABLE tenant_purchases
            ADD CONSTRAINT fk_purchase_verified_by
                FOREIGN KEY (verified_by)
                REFERENCES profile(id)
                ON DELETE SET NULL;
    END IF;
END $$;

-- Add constraints for valid values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'chk_package_condition'
    ) THEN
        ALTER TABLE tenant_purchases
            ADD CONSTRAINT chk_package_condition
                CHECK (package_condition IS NULL OR package_condition IN ('good', 'damaged', 'partial'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'chk_payment_method'
    ) THEN
        ALTER TABLE tenant_purchases
            ADD CONSTRAINT chk_payment_method
                CHECK (payment_method IS NULL OR payment_method IN (
                    'transfer', 'check', 'cash', 'credit_card', 'debit_card', 'other'
                ));
    END IF;
END $$;

-- Add comments
COMMENT ON COLUMN tenant_purchases.confirmation_number IS 'Supplier confirmation number';
COMMENT ON COLUMN tenant_purchases.tracking_number IS 'Shipping tracking/guide number';
COMMENT ON COLUMN tenant_purchases.carrier IS 'Shipping carrier/company name';

-- ============================================================================
-- 4. ADD RECEPTION FIELDS TO TENANT_PURCHASE_ITEMS
-- ============================================================================

ALTER TABLE tenant_purchase_items
    ADD COLUMN IF NOT EXISTS quantity_received NUMERIC(10, 3),
    ADD COLUMN IF NOT EXISTS quality_status VARCHAR(50),
    ADD COLUMN IF NOT EXISTS quality_notes TEXT,
    ADD COLUMN IF NOT EXISTS verification_notes TEXT,
    ADD COLUMN IF NOT EXISTS item_condition VARCHAR(50),

    -- Timestamps for item-level tracking
    ADD COLUMN IF NOT EXISTS received_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP WITH TIME ZONE;

-- Add constraints for valid values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'chk_quality_status'
    ) THEN
        ALTER TABLE tenant_purchase_items
            ADD CONSTRAINT chk_quality_status
                CHECK (quality_status IS NULL OR quality_status IN ('good', 'acceptable', 'poor', 'rejected'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'chk_item_condition'
    ) THEN
        ALTER TABLE tenant_purchase_items
            ADD CONSTRAINT chk_item_condition
                CHECK (item_condition IS NULL OR item_condition IN ('complete', 'partial', 'missing', 'damaged'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'chk_quantity_received_positive'
    ) THEN
        ALTER TABLE tenant_purchase_items
            ADD CONSTRAINT chk_quantity_received_positive
                CHECK (quantity_received IS NULL OR quantity_received >= 0);
    END IF;
END $$;

-- Add comments
COMMENT ON COLUMN tenant_purchase_items.quantity_received IS 'Actual quantity received (may differ from ordered quantity)';
COMMENT ON COLUMN tenant_purchase_items.quality_status IS 'Quality assessment: good, acceptable, poor, rejected';
COMMENT ON COLUMN tenant_purchase_items.item_condition IS 'Condition: complete, partial, missing, damaged';

-- ============================================================================
-- 5. CREATE TRIGGER FUNCTION FOR STATUS HISTORY
-- ============================================================================
-- Automatically log status changes to history table

CREATE OR REPLACE FUNCTION log_purchase_status_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Only log if status actually changed
    IF OLD.status IS DISTINCT FROM NEW.status THEN
        INSERT INTO purchase_status_history (
            purchase_id,
            tenant_id,
            from_status,
            to_status,
            changed_by,
            changed_at,
            notes
        ) VALUES (
            NEW.id,
            NEW.tenant_id,
            OLD.status,
            NEW.status,
            COALESCE(NEW.created_by, OLD.created_by), -- Use existing created_by if not updated
            NOW(),
            'Status changed from ' || COALESCE(OLD.status, 'null') || ' to ' || NEW.status
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create trigger
DROP TRIGGER IF EXISTS trg_purchase_status_change ON tenant_purchases;
CREATE TRIGGER trg_purchase_status_change
    AFTER UPDATE OF status ON tenant_purchases
    FOR EACH ROW
    EXECUTE FUNCTION log_purchase_status_change();

COMMENT ON FUNCTION log_purchase_status_change() IS 'Automatically logs all purchase status changes to purchase_status_history table';

-- ============================================================================
-- 6. CREATE VIEW FOR PURCHASE WITH LATEST STATUS
-- ============================================================================

CREATE OR REPLACE VIEW v_purchases_with_status AS
SELECT
    p.*,
    psh.changed_at as last_status_change,
    psh.changed_by as last_changed_by,
    psh.notes as last_status_notes,
    -- Count of attachments by type
    (SELECT COUNT(*) FROM purchase_attachments WHERE purchase_id = p.id) as attachment_count,
    -- Reception progress (items received / items total)
    (
        SELECT COUNT(*)
        FROM tenant_purchase_items
        WHERE purchase_id = p.id AND quantity_received IS NOT NULL
    ) as items_received_count,
    (
        SELECT COUNT(*)
        FROM tenant_purchase_items
        WHERE purchase_id = p.id
    ) as items_total_count,
    -- Calculate reception percentage
    CASE
        WHEN (SELECT COUNT(*) FROM tenant_purchase_items WHERE purchase_id = p.id) > 0
        THEN ROUND(
            (SELECT COUNT(*)::numeric FROM tenant_purchase_items WHERE purchase_id = p.id AND quantity_received IS NOT NULL) * 100.0 /
            (SELECT COUNT(*) FROM tenant_purchase_items WHERE purchase_id = p.id),
            2
        )
        ELSE 0
    END as reception_percentage
FROM tenant_purchases p
LEFT JOIN LATERAL (
    SELECT * FROM purchase_status_history
    WHERE purchase_id = p.id
    ORDER BY changed_at DESC
    LIMIT 1
) psh ON true;

COMMENT ON VIEW v_purchases_with_status IS 'Purchase orders with latest status info and reception progress metrics';

-- ============================================================================
-- 7. CREATE INDEXES FOR NEW FIELDS
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_purchases_tracking_number ON tenant_purchases(tracking_number) WHERE tracking_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchases_confirmation_number ON tenant_purchases(confirmation_number) WHERE confirmation_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchases_confirmed_at ON tenant_purchases(confirmed_at) WHERE confirmed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchases_shipped_at ON tenant_purchases(shipped_at) WHERE shipped_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchases_received_at ON tenant_purchases(received_at) WHERE received_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchase_items_received ON tenant_purchase_items(quantity_received) WHERE quantity_received IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_purchase_items_quality ON tenant_purchase_items(quality_status) WHERE quality_status IS NOT NULL;

COMMIT;

-- ============================================================================
-- ROLLBACK SCRIPT (for reference, run separately if needed)
-- ============================================================================

/*
BEGIN;

DROP VIEW IF EXISTS v_purchases_with_status;
DROP TRIGGER IF EXISTS trg_purchase_status_change ON tenant_purchases;
DROP FUNCTION IF EXISTS log_purchase_status_change();

ALTER TABLE tenant_purchase_items
    DROP COLUMN IF EXISTS quantity_received,
    DROP COLUMN IF EXISTS quality_status,
    DROP COLUMN IF EXISTS quality_notes,
    DROP COLUMN IF EXISTS verification_notes,
    DROP COLUMN IF EXISTS item_condition,
    DROP COLUMN IF EXISTS received_at,
    DROP COLUMN IF EXISTS verified_at;

ALTER TABLE tenant_purchases
    DROP CONSTRAINT IF EXISTS fk_purchase_received_by,
    DROP CONSTRAINT IF EXISTS fk_purchase_verified_by,
    DROP CONSTRAINT IF EXISTS chk_package_condition,
    DROP CONSTRAINT IF EXISTS chk_payment_method,
    DROP COLUMN IF EXISTS confirmation_number,
    DROP COLUMN IF EXISTS tracking_number,
    DROP COLUMN IF EXISTS carrier,
    DROP COLUMN IF EXISTS estimated_delivery_date,
    DROP COLUMN IF EXISTS package_count,
    DROP COLUMN IF EXISTS confirmed_at,
    DROP COLUMN IF EXISTS preparing_at,
    DROP COLUMN IF EXISTS shipped_at,
    DROP COLUMN IF EXISTS received_at,
    DROP COLUMN IF EXISTS verified_at,
    DROP COLUMN IF EXISTS invoiced_at,
    DROP COLUMN IF EXISTS paid_at,
    DROP COLUMN IF EXISTS cancelled_at,
    DROP COLUMN IF EXISTS payment_method,
    DROP COLUMN IF EXISTS payment_reference,
    DROP COLUMN IF EXISTS cancellation_reason,
    DROP COLUMN IF EXISTS received_by,
    DROP COLUMN IF EXISTS verified_by,
    DROP COLUMN IF EXISTS package_condition;

DROP TABLE IF EXISTS purchase_attachments;
DROP TABLE IF EXISTS purchase_status_history;

COMMIT;
*/
