-- Issue #146: kill write amplification on the sessions table.
--
-- These three indexes had 0 reads in pg_stat_user_indexes (verified 2026-05-02);
-- they only existed to be updated, costing 6× write amplification per UPDATE.
-- Dropping idx_sessions_last_activity additionally enables HOT updates because
-- it was the only indexed column actually changing in the per-request UPDATE.
--
-- Run order: this file is safe to apply at any time of day. CONCURRENTLY makes
-- the DROPs non-blocking; existing reads/writes are unaffected during the drop.
--
-- Rollback (if any future feature needs these indexes):
--   CREATE INDEX CONCURRENTLY idx_sessions_last_activity ON public.sessions (last_activity_at);
--   CREATE INDEX CONCURRENTLY idx_sessions_created_at    ON public.sessions (created_at);
--   CREATE INDEX CONCURRENTLY idx_sessions_ip_address    ON public.sessions (ip_address);
--   ALTER TABLE public.sessions RESET (
--     autovacuum_vacuum_scale_factor,
--     autovacuum_analyze_scale_factor,
--     autovacuum_vacuum_cost_limit
--   );

DROP INDEX CONCURRENTLY IF EXISTS public.idx_sessions_last_activity;
DROP INDEX CONCURRENTLY IF EXISTS public.idx_sessions_created_at;
DROP INDEX CONCURRENTLY IF EXISTS public.idx_sessions_ip_address;

-- Sessions is high-churn write-heavy. Tighten autovacuum locally so dead tuples
-- are reclaimed at 5% bloat instead of the 20% global default.
ALTER TABLE public.sessions SET (
  autovacuum_vacuum_scale_factor = 0.05,
  autovacuum_analyze_scale_factor = 0.05,
  autovacuum_vacuum_cost_limit = 2000
);
