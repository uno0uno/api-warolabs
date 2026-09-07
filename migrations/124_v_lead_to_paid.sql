-- Issue #921: measurable funnel view (additive only, no DROP)
CREATE OR REPLACE VIEW v_lead_to_paid AS
WITH last_li AS (
    SELECT DISTINCT ON (l.id)
        l.id AS lead_id,
        l.profile_id, l.email AS lead_email, l.source AS lead_source, l.status AS lead_status,
        l.created_at AS lead_at, l.utm_source, l.utm_medium, l.utm_campaign, l.utm_term, l.utm_content,
        li.id AS interaction_id, li.interaction_type, li.source AS interaction_source,
        li.visitor_key, li.campaign_id, li.medium, li.campaign AS li_campaign, li.term, li.content,
        li.ip_address, li.created_at AS interaction_at
    FROM leads l
    JOIN lead_interactions li ON li.lead_id = l.id
    ORDER BY l.id, li.created_at DESC
)
SELECT
    p.id AS profile_id, p.email AS profile_email,
    ll.lead_id, ll.lead_source, ll.lead_status, ll.lead_at,
    ll.utm_source AS lead_utm_source, ll.utm_medium AS lead_utm_medium, ll.utm_campaign AS lead_utm_campaign,
    ll.utm_term AS lead_utm_term, ll.utm_content AS lead_utm_content,
    ll.interaction_id, ll.interaction_type, ll.interaction_source, ll.visitor_key,
    ll.campaign_id, c.slug AS campaign_slug, c.name AS campaign_name,
    ll.medium AS li_medium, ll.li_campaign, ll.term, ll.content,
    te.path AS first_trail_path, te.referrer AS first_referrer,
    te.utm_source AS trail_utm_source, te.utm_medium AS trail_utm_medium, te.utm_campaign AS trail_utm_campaign,
    te.occurred_at AS first_trail_at,
    toe.state AS onboarding_state, t.id AS tenant_id, t.lifecycle_status, t.name AS tenant_name,
    bpa.id AS attempt_id, bpa.status AS attempt_status, bpa.provider, bpa.expected_amount_in_cents, bpa.currency, bpa.created_at AS attempt_at,
    bpa.visitor_key AS attempt_visitor_key, bpa.lead_id AS attempt_lead_id,
    ts.id AS subscription_id, ts.status AS subscription_status, ts.billing_cycle, ts.current_period_end
FROM profile p
JOIN last_li ll ON ll.profile_id = p.id
LEFT JOIN campaign c ON c.id = ll.campaign_id
LEFT JOIN LATERAL (
    SELECT path, referrer, utm_source, utm_medium, utm_campaign, occurred_at
    FROM trail_events
    WHERE visitor_key = ll.visitor_key
    ORDER BY occurred_at ASC
    LIMIT 1
) te ON ll.visitor_key IS NOT NULL
LEFT JOIN tenant_onboarding toe ON toe.verified_email = lower(trim(p.email)) OR toe.owner_user_id = p.id
LEFT JOIN tenants t ON t.id = toe.tenant_id
LEFT JOIN LATERAL (
    SELECT id, status, provider, expected_amount_in_cents, currency, created_at, visitor_key, lead_id
    FROM billing_payment_attempts
    WHERE tenant_id = t.id
    ORDER BY created_at DESC
    LIMIT 1
) bpa ON true
LEFT JOIN tenant_subscriptions ts ON ts.tenant_id = t.id;

COMMENT ON VIEW v_lead_to_paid IS 'Issue #921 funnel: profile→leads→last lead_interactions→campaign→first trail_events LATERAL→onboarding→last payment attempt→subscription';
