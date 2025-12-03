-- Migration: Add product_base_recipes junction table
-- Description: Allows products to have multiple recipe bases instead of just one
-- Date: 2025-12-02

-- Create junction table for many-to-many relationship between products and recipe bases
CREATE TABLE IF NOT EXISTS product_base_recipes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES product(id) ON DELETE CASCADE,
    product_base_type_id UUID NOT NULL REFERENCES product_base_types(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, product_base_type_id, tenant_id)
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_product_base_recipes_product_id ON product_base_recipes(product_id);
CREATE INDEX IF NOT EXISTS idx_product_base_recipes_base_type_id ON product_base_recipes(product_base_type_id);
CREATE INDEX IF NOT EXISTS idx_product_base_recipes_tenant_id ON product_base_recipes(tenant_id);

-- Add comment
COMMENT ON TABLE product_base_recipes IS 'Junction table linking products to multiple recipe bases';

-- Migrate existing data from product.product_base_type_id to the new table
INSERT INTO product_base_recipes (product_id, product_base_type_id, tenant_id)
SELECT id, product_base_type_id, tenant_id
FROM product
WHERE product_base_type_id IS NOT NULL
ON CONFLICT (product_id, product_base_type_id, tenant_id) DO NOTHING;

-- Note: We keep the product_base_type_id column in the product table for now
-- to maintain backward compatibility. It can be removed in a future migration.
