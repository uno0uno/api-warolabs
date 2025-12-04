-- Migration: Add weight tracking fields to tenant_purchase_items
-- Date: 2025-12-04
-- Description: Add support for tracking package weight information
--              to enable detailed weight-per-unit calculations

-- Add weight tracking columns to tenant_purchase_items table
ALTER TABLE tenant_purchase_items
ADD COLUMN IF NOT EXISTS weight_value NUMERIC,
ADD COLUMN IF NOT EXISTS weight_unit VARCHAR(10),
ADD COLUMN IF NOT EXISTS weight_per_unit_grams NUMERIC(10,2);

-- Add comments to document the columns
COMMENT ON COLUMN tenant_purchase_items.weight_value IS 'Weight value of the package (e.g., 1, 2.5)';
COMMENT ON COLUMN tenant_purchase_items.weight_unit IS 'Weight unit (gr, kg, lb, oz)';
COMMENT ON COLUMN tenant_purchase_items.weight_per_unit_grams IS 'Calculated weight per individual unit in grams';

-- Example usage:
-- For a package of 18 sausages weighing 1 kg:
-- weight_value = 1
-- weight_unit = 'kg'
-- weight_per_unit_grams = 55.56 (1000g / 18 units)
