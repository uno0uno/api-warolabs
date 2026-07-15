# Trial lifecycle rollout

Issue: `uno0uno/api-warolabs#637`

## Deployment order

1. Merge and deploy #636, applying migrations `106` through `109` in numeric
   order.
2. Apply migration `110` before deploying the #637 API image. The migration is
   additive until the new code starts creating trials. Confirm `110` adds
   `trialing`, `trial_expired`, both trial timestamps, and the partial event
   uniqueness index.
3. Deploy the #637 API image.
4. Set `CRON_SECRET` on the API. The trial lifecycle endpoint fails closed when
   this value is absent.
5. Configure one daily trigger for:

   ```text
   POST https://api.warolabs.com/billing/process-trial-lifecycle
   x-cron-secret: <CRON_SECRET>
   ```

   The API owns selection, SES delivery, deduplication, and expiry. The
   scheduler must remain a trigger and must not duplicate those rules.
6. The existing `/home/saifer/warocol-scheduler` container currently runs with
   `DRY_RUN=true`. Set its effective `DRY_RUN=false` before relying on its paid
   subscription reminders. Restart only that service and verify the startup log
   no longer reports dry-run mode.

## Smoke verification

- Create a disposable trial fixture ending in a 7-day warning window.
- Invoke the endpoint once and confirm `sent=1`, an SES `MessageId`, and one
  `trial_warning_day_7` event.
- Confirm event metadata contains only `days_remaining` and `trial_ends_at`,
  never email or phone.
- Invoke the endpoint again and confirm the warning is skipped with no second
  event or SES delivery.
- Move the fixture to the exact expiry boundary, invoke again, and confirm
  `trial_expired`, read-only access, and no paid `past_due` grace state.
- Approve a Wompi fixture after expiry and confirm the same subscription becomes
  `active` with its paid period anchored to Wompi.

## Rollback

Before any trial is activated, use the rollback statements documented at the
bottom of `migrations/110_subscription_trial_lifecycle.sql` and deploy the prior
API image. After trial rows exist, do not drop the columns or status constraint:
disable the trigger, roll forward with a corrective migration, and preserve all
subscription and billing-event evidence.
