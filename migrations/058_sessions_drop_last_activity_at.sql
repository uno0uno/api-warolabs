-- Issue #148: remove the dead last_activity_at column from sessions.
--
-- Verified during research: 0 read consumers in backend or frontend, 0 cron
-- jobs depend on it, 0 features (idle-timeout, billing, UI display) use it.
-- After PR #147 the writes were throttled by 95%; this migration completes
-- the cleanup by removing the column entirely.
--
-- The view `public.active_sessions` (a DBA convenience for inspecting active
-- sessions sorted by last_activity_at) depends on the column. No application
-- code references the view (only the literal string "active_sessions" appears
-- as a Python variable name in magic_link_service.py — the queries hit the
-- `sessions` table directly). The view is dropped here. If a DBA wants similar
-- inspection going forward, they can use a direct `SELECT FROM sessions`.
--
-- DROP COLUMN in Postgres 16 is a metadata-only operation (instant on a
-- 39-row table). The brief AccessExclusive lock is sub-millisecond.
--
-- Rollback (data loss is acceptable — historical timestamps had no consumer):
--   ALTER TABLE public.sessions ADD COLUMN last_activity_at timestamptz DEFAULT now();
--   CREATE VIEW public.active_sessions AS
--     SELECT s.id, s.user_id, p.email, p.name, s.created_at, s.last_activity_at,
--            s.expires_at, s.ip_address, s.user_agent, s.login_method,
--            EXTRACT(epoch FROM now() - s.last_activity_at) / 60::numeric AS minutes_since_last_activity
--     FROM sessions s
--     JOIN profile p ON s.user_id = p.id
--     WHERE s.is_active = true AND s.expires_at > now()
--     ORDER BY s.last_activity_at DESC;

DROP VIEW IF EXISTS public.active_sessions;
ALTER TABLE public.sessions DROP COLUMN IF EXISTS last_activity_at;
