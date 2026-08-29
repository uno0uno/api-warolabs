-- warocol.com#2509 — POS category card color (ADD-only)
ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS color VARCHAR(7);

COMMENT ON COLUMN categories.color IS
    'Optional POS catalog card color as #RRGGBB. Tenant-owned categories only; null = keyword/neutral fallback.';
