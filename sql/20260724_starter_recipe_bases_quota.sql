-- warocol.com#1800: add a Starter recipe-bases growth cap.
-- Non-destructive: merge only this catalog quota key into the Starter plan.

UPDATE subscription_plans
SET
    features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{quotas}',
        COALESCE(features->'quotas', '{}'::jsonb) || '{
          "recipe_bases": 5
        }'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'starter';
