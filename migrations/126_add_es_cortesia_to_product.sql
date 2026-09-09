-- uno0uno/warocol.com#2654: courtesy flag for zero-price products (ADD-only)
-- Existing rows default FALSE; courtesy-only price 0 enforced in app.
ALTER TABLE product
    ADD COLUMN IF NOT EXISTS es_cortesia boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN product.es_cortesia IS
    'When true, the product may be sold at price 0 (cortesia). Price 0 without this flag is rejected (uno0uno/warocol.com#2654).';
