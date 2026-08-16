-- Attach landing-only templates to food-cost and ia-factura squeeze pages.
-- Additive INSERT only. No DROP/ALTER. No new campaign columns. Email pair not required.

DO $$
DECLARE
  v_profile uuid := '7fe92b2c-d99e-4c70-b0cb-74af6326da5a';
  v_campaign uuid;
  v_template uuid;
  v_version uuid;
BEGIN
  SELECT id INTO v_campaign
  FROM campaign
  WHERE slug = 'food-cost'
    AND profile_id = v_profile
    AND coalesce(is_deleted, false) = false;

  IF v_campaign IS NULL THEN
    RAISE NOTICE 'food-cost campaign missing, skip';
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM campaign_template_versions ctv
    JOIN template_versions tv ON tv.id = ctv.template_version_id
    JOIN templates t ON t.id = tv.template_id
    WHERE ctv.campaign_id = v_campaign
      AND t.template_type = 'landing'
      AND coalesce(t.is_deleted, false) = false
  ) THEN
    RAISE NOTICE 'food-cost already has a landing template';
    RETURN;
  END IF;

  INSERT INTO templates (template_name, template_type, description, created_by_profile_id)
  VALUES (
    'Deja de vender a ciegas - Landing',
    'landing',
    'Squeeze page copy for /landing/food-cost',
    v_profile
  )
  RETURNING id INTO v_template;

  INSERT INTO template_versions (template_id, version_number, content)
  VALUES (
    v_template,
    1,
    '{"title":"Deja de vender a ciegas","description":"El POS cobró el plato. No descontó los gramos.","cta_label":"Quiero ver mi food cost","microcopy":"Te escribimos por WhatsApp al número que dejes."}'
  )
  RETURNING id INTO v_version;

  UPDATE templates SET active_version_id = v_version WHERE id = v_template;

  INSERT INTO campaign_template_versions (campaign_id, template_version_id, is_active)
  VALUES (v_campaign, v_version, true);
END $$;

DO $$
DECLARE
  v_profile uuid := '7fe92b2c-d99e-4c70-b0cb-74af6326da5a';
  v_campaign uuid;
  v_template uuid;
  v_version uuid;
BEGIN
  SELECT id INTO v_campaign
  FROM campaign
  WHERE slug = 'ia-factura'
    AND profile_id = v_profile
    AND coalesce(is_deleted, false) = false;

  IF v_campaign IS NULL THEN
    RAISE NOTICE 'ia-factura campaign missing, skip';
    RETURN;
  END IF;

  IF EXISTS (
    SELECT 1
    FROM campaign_template_versions ctv
    JOIN template_versions tv ON tv.id = ctv.template_version_id
    JOIN templates t ON t.id = tv.template_id
    WHERE ctv.campaign_id = v_campaign
      AND t.template_type = 'landing'
      AND coalesce(t.is_deleted, false) = false
  ) THEN
    RAISE NOTICE 'ia-factura already has a landing template';
    RETURN;
  END IF;

  INSERT INTO templates (template_name, template_type, description, created_by_profile_id)
  VALUES (
    'Una foto. El inventario al día - Landing',
    'landing',
    'Squeeze page copy for /landing/ia-factura',
    v_profile
  )
  RETURNING id INTO v_template;

  INSERT INTO template_versions (template_id, version_number, content)
  VALUES (
    v_template,
    1,
    '{"title":"Una foto. El inventario al día","description":"Deja de digitar la factura del proveedor. Una foto y el inventario queda al día.","cta_label":"Quiero dejar de digitar","microcopy":"Te escribimos por WhatsApp al número que dejes."}'
  )
  RETURNING id INTO v_version;

  UPDATE templates SET active_version_id = v_version WHERE id = v_template;

  INSERT INTO campaign_template_versions (campaign_id, template_version_id, is_active)
  VALUES (v_campaign, v_version, true);
END $$;
