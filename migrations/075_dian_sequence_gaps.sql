-- 075_dian_sequence_gaps.sql
-- Issue warocol.com#592 — auto-skip + retry on Matias "ya validado" with
-- multi-resolution fallback. This migration installs the foundations the
-- api-facturacion retry loop will rely on.
--
-- Forward-only constraint clarification from #592 design review:
--   "Reusing numbering that has been reversed by annulment is an
--    infraction subject to sanctions (DIAN)."
--   → current_number must only advance. Never rewind. Never reuse a
--     number that was logged in dian_sequence_gaps.
--
-- Schema changes (all non-destructive ADD/CREATE per CLAUDE.md):
--   1. CHECK constraint on dian_resolutions.current_number range.
--   2. BEFORE UPDATE trigger blocking any decrement.
--   3. Drop the old single-active UNIQUE index; allow multi-active per
--      (tenant, prefix) ordered by a new `priority` column. (DROP INDEX
--      is non-destructive in data terms — no rows deleted.)
--   4. New `priority` column on dian_resolutions (default 100).
--   5. New `dian_sequence_gaps` audit table with UNIQUE
--      (tenant_id, prefix, skipped_number) to enforce single-use globally.
--
-- See: https://github.com/uno0uno/warocol.com/issues/592

-- 1. Counter range check.
ALTER TABLE dian_resolutions
  ADD CONSTRAINT dian_resolutions_counter_within_range
  CHECK (current_number >= from_number - 1 AND current_number <= to_number);

-- 2. Forward-only counter trigger.
CREATE OR REPLACE FUNCTION dian_resolutions_no_rewind() RETURNS trigger AS $$
BEGIN
  IF NEW.current_number < OLD.current_number THEN
    RAISE EXCEPTION
      'current_number cannot decrease (was %, attempted %). DIAN forbids number reuse.',
      OLD.current_number, NEW.current_number
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER dian_resolutions_no_rewind_trigger
  BEFORE UPDATE OF current_number ON dian_resolutions
  FOR EACH ROW EXECUTE FUNCTION dian_resolutions_no_rewind();

-- 3 + 4. Multi-resolution fallback: drop the single-active UNIQUE
--   index and add `priority`. Highest priority wins when api-facturacion
--   allocates the next number.
DROP INDEX IF EXISTS idx_dian_resolutions_active_prefix;

ALTER TABLE dian_resolutions
  ADD COLUMN priority integer NOT NULL DEFAULT 100;

CREATE INDEX idx_dian_resolutions_active_priority
  ON dian_resolutions(tenant_id, prefix, priority DESC, from_number ASC)
  WHERE is_active = true;

-- 5. Sequence gaps audit table.
CREATE TABLE dian_sequence_gaps (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id                 uuid NOT NULL REFERENCES tenants(id),
  resolution_id             uuid NOT NULL REFERENCES dian_resolutions(id),
  prefix                    varchar(10) NOT NULL,
  skipped_number            integer NOT NULL,
  reason                    varchar(50) NOT NULL,
  matias_response           jsonb,
  original_attempt_order_id uuid REFERENCES orders(id),
  created_at                timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dian_gaps_no_reuse UNIQUE (tenant_id, prefix, skipped_number)
);

CREATE INDEX idx_dian_gaps_tenant_created ON dian_sequence_gaps(tenant_id, created_at DESC);
CREATE INDEX idx_dian_gaps_resolution ON dian_sequence_gaps(resolution_id);

COMMENT ON TABLE dian_sequence_gaps IS
  'Audit trail of DIAN invoice numbers that were allocated but never accepted '
  'by Matias/DIAN. Each row represents a number permanently retired from the '
  'sequence — DIAN forbids reuse so this table is the legal justification '
  'for any gap an auditor finds. See warocol.com#592.';

COMMENT ON COLUMN dian_sequence_gaps.reason IS
  'Why the number was skipped: matias_ya_validado, matias_500, network_timeout, etc.';

COMMENT ON COLUMN dian_sequence_gaps.matias_response IS
  'Full Matias error payload at the time of skip — DIAN audit-grade evidence.';
