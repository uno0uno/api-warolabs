-- warocol.com#1732 — editorial pillar id for blog magazine grouping (additive only)

ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS pillar text NULL;

CREATE INDEX IF NOT EXISTS idx_articles_tenant_pillar
    ON articles (tenant_id, pillar)
    WHERE published = true AND is_active = true;

COMMENT ON COLUMN articles.pillar IS
    'Editorial pillar id from content/pillar--*/ (e.g. software-para-restaurantes).';
