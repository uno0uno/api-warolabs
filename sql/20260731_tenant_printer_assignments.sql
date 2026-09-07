-- warocol.com#1949 — tenant printer routing (caja + per kitchen station)
-- ADD/CREATE only. Printer names are OS/QZ display names (stable key).

CREATE TABLE IF NOT EXISTS tenant_printer_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    role text NOT NULL CHECK (role IN ('caja', 'station')),
    station_id uuid REFERENCES kitchen_stations(id) ON DELETE CASCADE,
    printer_name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenant_printer_assignments_role_station_chk CHECK (
        (role = 'caja' AND station_id IS NULL)
        OR (role = 'station' AND station_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_printer_assignments_caja
    ON tenant_printer_assignments (tenant_id)
    WHERE role = 'caja';

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_printer_assignments_station
    ON tenant_printer_assignments (tenant_id, station_id)
    WHERE role = 'station';

CREATE INDEX IF NOT EXISTS idx_tenant_printer_assignments_tenant
    ON tenant_printer_assignments (tenant_id);

COMMENT ON TABLE tenant_printer_assignments IS
    'POS printer routing: one caja printer + optional per kitchen_station (#1949).';
