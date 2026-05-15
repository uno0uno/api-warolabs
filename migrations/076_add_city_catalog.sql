-- 076_add_city_catalog.sql
-- Issue warocol.com#615 — Dynamic country/city directory
--
-- Replaces the hardcoded /bogota directory with a curated catalog. Adds a
-- normalized city_slug + country to tenant_public_profiles and creates a
-- public_cities table seeded with the Colombian cities WARO operates in.
--
-- ADD-only per CLAUDE.md — no DROP / destructive ALTER. Existing city
-- VARCHAR stays as the canonical display name; city_slug is the routing key.
--
-- Pre-deploy safety check (run on prod BEFORE applying — must return 0 rows):
--   SELECT t.slug FROM tenants t
--   WHERE t.slug IN ('bogota','medellin','cali','barranquilla','cartagena',
--                    'bucaramanga','pereira','manizales','mosquera',
--                    'santa-fe-de-antioquia');

CREATE EXTENSION IF NOT EXISTS unaccent;

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS country VARCHAR(80) DEFAULT 'Colombia';

ALTER TABLE tenant_public_profiles
    ADD COLUMN IF NOT EXISTS city_slug VARCHAR(120);

COMMENT ON COLUMN tenant_public_profiles.country IS
    'Country where the business operates (warocol.com#615). v1 locks the '
    'operator UI to Colombia; the column is plain VARCHAR so future countries '
    'can be added without a type change.';

COMMENT ON COLUMN tenant_public_profiles.city_slug IS
    'Normalized slug for the business city (warocol.com#615). References '
    'public_cities.city_slug; used as the routing key on warocol.com/{slug} '
    'and as the directory filter. The display name lives in the city column.';

CREATE INDEX IF NOT EXISTS idx_tpp_city_slug_active
    ON tenant_public_profiles(city_slug)
    WHERE is_active = true;

CREATE TABLE IF NOT EXISTS public_cities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    country     VARCHAR(80) NOT NULL DEFAULT 'Colombia',
    city        VARCHAR(120) NOT NULL,
    city_slug   VARCHAR(120) NOT NULL UNIQUE,
    is_active   BOOLEAN NOT NULL DEFAULT true,
    sort_order  INTEGER NOT NULL DEFAULT 100,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public_cities IS
    'Curated catalog of cities WARO operates in (warocol.com#615). Used to '
    'populate the city selector on /negocio, the discovery section on /, and '
    'the reserved-words check in tenants_service._generate_slug. Adding a '
    'city here makes warocol.com/{city_slug} reachable; removing it requires '
    'reassigning any tenants that reference it.';

CREATE INDEX IF NOT EXISTS idx_public_cities_active
    ON public_cities(is_active, sort_order)
    WHERE is_active = true;

INSERT INTO public_cities (country, city, city_slug, sort_order)
VALUES
    ('Colombia', 'Bogotá',                  'bogota',                  10),
    ('Colombia', 'Medellín',                'medellin',                20),
    ('Colombia', 'Cali',                    'cali',                    30),
    ('Colombia', 'Barranquilla',            'barranquilla',            40),
    ('Colombia', 'Cartagena',               'cartagena',               50),
    ('Colombia', 'Bucaramanga',             'bucaramanga',             60),
    ('Colombia', 'Pereira',                 'pereira',                 70),
    ('Colombia', 'Manizales',               'manizales',               80),
    ('Colombia', 'Mosquera',                'mosquera',                90),
    ('Colombia', 'Santa Fe de Antioquia',   'santa-fe-de-antioquia',  100)
ON CONFLICT (city_slug) DO NOTHING;

-- Backfill city_slug for existing rows whose city column matches a seeded
-- entry — case-insensitive + accent-insensitive, since prod has values like
-- 'MOSQUERA' and 'SANTA FE ANTIOQUIA' (missing 'de', wrong case).
UPDATE tenant_public_profiles tpp
SET city_slug = pc.city_slug,
    city = pc.city
FROM public_cities pc
WHERE tpp.city_slug IS NULL
  AND tpp.city IS NOT NULL
  AND tpp.city != ''
  AND (
        lower(unaccent(tpp.city)) = lower(unaccent(pc.city))
        OR lower(unaccent(tpp.city)) = pc.city_slug
        OR lower(unaccent(tpp.city)) LIKE lower(unaccent(pc.city)) || '%'
        OR lower(unaccent(pc.city)) LIKE lower(unaccent(tpp.city)) || '%'
      );
