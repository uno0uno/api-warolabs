-- warocol.com#2572 — deduct on command is opt-in (ADD-safe)
ALTER TABLE tenant_public_profiles
    ALTER COLUMN deduct_inventory_on_command SET DEFAULT false;

UPDATE tenant_public_profiles
SET deduct_inventory_on_command = false
WHERE deduct_inventory_on_command IS DISTINCT FROM false;

COMMENT ON COLUMN tenant_public_profiles.deduct_inventory_on_command IS
    'When true, POS mesa/tab and QR/delivery accept deduct inventory on command/accept; COGS still posts at checkout. Default false (opt-in). warocol.com#2572';
