-- Issue #151: subscription lifecycle cron.
--
-- Voluntary-renewal model: when a tenant pays, current_period_end is set to
-- NOW() + interval. When that date arrives, nothing transitions the row.
-- This cron does the time-based transitions that the existing webhook-driven
-- code paths cannot.
--
-- Schedule: 08:00 UTC daily (= 03:00 Bogotá), well outside any traffic peak.
-- pg_cron 1.6 already runs jobs in this DB (see `update_reservations`).
--
-- Two transitions:
--   1. status='active'   AND period_end <  NOW()                 → past_due
--   2. status='past_due' AND period_end <  NOW() - 7 days         → cancelled
--
-- For each transition we INSERT a billing_events row with idempotency:
-- the WHERE NOT EXISTS clause prevents duplicate events when the cron
-- runs more than once in a day for any reason.
--
-- Email sending is OUT OF SCOPE here — handled by a separate cron/process
-- that reads from billing_events / past_due rows.
--
-- Rollback:
--   SELECT cron.unschedule('subscription_lifecycle_daily');

SELECT cron.schedule(
  'subscription_lifecycle_daily',
  '0 8 * * *',
  $job$
    -- 1. active → past_due when period ended
    WITH expired AS (
      UPDATE tenant_subscriptions
      SET status = 'past_due', updated_at = NOW()
      WHERE status = 'active'
        AND current_period_end < NOW()
      RETURNING id, tenant_id, current_period_end
    )
    INSERT INTO billing_events (tenant_id, subscription_id, event_type, metadata)
    SELECT
      tenant_id,
      id,
      'subscription_period_ended',
      jsonb_build_object(
        'period_end', current_period_end::text,
        'days_overdue', GREATEST(0, EXTRACT(DAY FROM (NOW() - current_period_end))::int),
        'transition', 'active_to_past_due'
      )
    FROM expired
    WHERE NOT EXISTS (
      SELECT 1 FROM billing_events
      WHERE subscription_id = expired.id
        AND event_type = 'subscription_period_ended'
        AND created_at >= expired.current_period_end
    );

    -- 2. past_due > grace period (7 days) → cancelled (auto-block)
    WITH grace_exhausted AS (
      UPDATE tenant_subscriptions
      SET status = 'cancelled',
          cancelled_at = NOW(),
          updated_at = NOW()
      WHERE status = 'past_due'
        AND current_period_end < NOW() - INTERVAL '7 days'
      RETURNING id, tenant_id, current_period_end
    )
    INSERT INTO billing_events (tenant_id, subscription_id, event_type, metadata)
    SELECT
      tenant_id,
      id,
      'subscription_auto_cancelled',
      jsonb_build_object(
        'period_end', current_period_end::text,
        'days_overdue', EXTRACT(DAY FROM (NOW() - current_period_end))::int,
        'reason', 'grace_period_exhausted',
        'grace_days', 7
      )
    FROM grace_exhausted
    WHERE NOT EXISTS (
      SELECT 1 FROM billing_events
      WHERE subscription_id = grace_exhausted.id
        AND event_type = 'subscription_auto_cancelled'
    );
  $job$
);
