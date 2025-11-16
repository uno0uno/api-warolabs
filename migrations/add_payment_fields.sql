-- Add missing payment fields to tenant_purchases table
-- Date: 2025-11-16
-- Purpose: Support payment_amount and payment_date fields for purchase payments

ALTER TABLE tenant_purchases
ADD COLUMN IF NOT EXISTS payment_amount NUMERIC,
ADD COLUMN IF NOT EXISTS payment_date TIMESTAMP WITH TIME ZONE;

-- Add comments for documentation
COMMENT ON COLUMN tenant_purchases.payment_amount IS 'Actual amount paid for this purchase';
COMMENT ON COLUMN tenant_purchases.payment_date IS 'Date when payment was made';

-- Verify columns were added
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'tenant_purchases'
AND column_name IN ('payment_amount', 'payment_date')
ORDER BY column_name;
