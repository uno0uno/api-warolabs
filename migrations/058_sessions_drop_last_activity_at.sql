-- Issue #148: remove the dead last_activity_at column from sessions.
--
-- Verified during research: 0 read consumers in backend or frontend, 0 cron
-- jobs depend on it, 0 features (idle-timeout, billing, UI display) use it.
-- After PR #147 the writes were throttled by 95%; this migration completes
-- the cleanup by removing the column entirely.
--
-- DROP COLUMN in Postgres 16 is a metadata-only operation (instant on a
-- 39-row table). The brief AccessExclusive lock is sub-millisecond.
--
-- Rollback (data loss is acceptable — historical timestamps had no consumer):
--   ALTER TABLE public.sessions ADD COLUMN last_activity_at timestamptz DEFAULT now();

ALTER TABLE public.sessions DROP COLUMN IF EXISTS last_activity_at;
