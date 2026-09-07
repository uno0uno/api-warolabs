-- warocol.com#2459: additive ISO country for articles (keep articles.country text)
ALTER TABLE articles
    ADD COLUMN IF NOT EXISTS country_code CHAR(2);

-- Idempotent backfill from free-text country. LATAM / unknown stay NULL (es+COP fallback in app).
UPDATE articles
SET country_code = CASE
    WHEN country_code IS NOT NULL AND btrim(country_code) <> '' THEN upper(btrim(country_code))
    WHEN upper(btrim(country)) IN (
        'US','CA','GB','AU','NZ','BR','DE','FR','NL','SG','AE','IN','CN',
        'MX','ES','CO','CR','UY','CL','PE','AR','DO','PA'
    ) THEN upper(btrim(country))
    WHEN lower(btrim(country)) IN ('usa', 'u.s.', 'u.s.a.', 'united states', 'united states of america') THEN 'US'
    WHEN lower(btrim(country)) IN ('colombia', 'col') THEN 'CO'
    WHEN lower(btrim(country)) IN ('spain', 'españa', 'espana') THEN 'ES'
    WHEN lower(btrim(country)) IN ('mexico', 'méxico') THEN 'MX'
    ELSE NULL
END
WHERE country_code IS NULL OR btrim(coalesce(country_code, '')) = '';
