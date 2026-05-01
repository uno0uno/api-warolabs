-- Issue #465: Single product image
--
-- Mirrors the tenant_public_profiles.{logo_url, banner_url} pattern. The URL
-- points to a public Cloudflare R2 object created via POST /api/menu/products/upload-image.
-- Existing rows stay NULL; the frontend falls back to the emoji previously
-- assigned per category in the POS grid.

ALTER TABLE product
    ADD COLUMN IF NOT EXISTS image_url varchar(500) NULL;
