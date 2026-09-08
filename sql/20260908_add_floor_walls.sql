-- uno0uno/warocol.com#2614 — floor-plan walls/obstacles per tenant.
-- Additive only: new table, no changes to existing tables.

CREATE TABLE IF NOT EXISTS floor_walls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL,
    zona varchar(50) NOT NULL,
    x1 double precision NOT NULL,
    y1 double precision NOT NULL,
    x2 double precision NOT NULL,
    y2 double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE floor_walls IS
    'Optional floor-plan wall segments per tenant (POS visual reference). Empty = no walls, floor renders as today.';
COMMENT ON COLUMN floor_walls.zona IS
    'Zone the wall belongs to; matches tables.zona vocabulary (NULL-zone tables render in Sin ubicar).';

CREATE INDEX IF NOT EXISTS idx_floor_walls_tenant_zona
    ON floor_walls (tenant_id, zona);
