-- warocol.com#2566 — deduct warehouse stock when items are sent to kitchen (ADD-only)
ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS deduct_inventory_on_command boolean NOT NULL DEFAULT true;

COMMENT ON COLUMN tenant_public_profiles.deduct_inventory_on_command IS
    'When true, POS mesa/tab (and later QR/delivery accept) deducts inventory on command/accept; COGS still posts at checkout (warocol.com#2566).';
