-- warocol.com#1798: add a Starter category cap and raise modifier groups.
-- Non-destructive: merge only these catalog quota keys into the Starter plan.

UPDATE subscription_plans
SET
    features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{quotas}',
        COALESCE(features->'quotas', '{}'::jsonb) || '{
          "menu_categories": 5,
          "modifier_groups": 4
        }'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'starter';
