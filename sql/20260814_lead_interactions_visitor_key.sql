-- uno0uno/waro-trail#4: join captured leads to first-party trail visitors
ALTER TABLE lead_interactions
    ADD COLUMN IF NOT EXISTS visitor_key TEXT;

CREATE INDEX IF NOT EXISTS idx_lead_interactions_visitor_key
    ON lead_interactions (visitor_key)
    WHERE visitor_key IS NOT NULL;

COMMENT ON COLUMN lead_interactions.visitor_key IS
    'Opaque first-party cookie id (waro_visitor_key); no FK to trail_visitors.';
