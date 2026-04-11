-- Migration 007: Add exact timestamps to accounting_period
-- Allows cierre periods to be defined by exact hour (e.g. 10pm–10am shifts).
-- Columns are nullable — existing rows keep NULL, queried with date fallback.

ALTER TABLE accounting_period
    ADD COLUMN IF NOT EXISTS period_start_time TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS period_end_time   TIMESTAMPTZ NULL;

COMMENT ON COLUMN accounting_period.period_start_time IS
    'Exact start timestamp for the period (optional). When set, order filtering uses this instead of period_start::date.';
COMMENT ON COLUMN accounting_period.period_end_time IS
    'Exact end timestamp for the period (optional). When set, order filtering uses this instead of period_end::date.';
