-- api-warolabs#694: Starter catalog quota keys (non-destructive).
-- Merges catalog caps into the starter plan features.quotas JSON.

UPDATE subscription_plans
SET
    features = jsonb_set(
        COALESCE(features, '{}'::jsonb),
        '{quotas}',
        COALESCE(features->'quotas', '{}'::jsonb) || '{
          "menu_products": 10,
          "tenant_ingredients": 5,
          "modifier_groups": 2,
          "recipe_lines_per_product": 4,
          "modifier_options_per_group": 6,
          "recipe_base_template_lines": 4
        }'::jsonb,
        true
    ),
    updated_at = now()
WHERE slug = 'starter';
