-- warocol.com#2295: country-scoped city catalog (CO backfill + short AR/MX/US lists).
-- Additive only. Keep UNIQUE(city_slug). Extra-country slugs must not reuse CO slugs.

ALTER TABLE public_cities
  ADD COLUMN IF NOT EXISTS country_code VARCHAR(2) NOT NULL DEFAULT 'CO';

CREATE INDEX IF NOT EXISTS idx_public_cities_country_code_active
  ON public_cities (country_code, sort_order)
  WHERE is_active = true;

INSERT INTO public_cities (
  country, country_code, city, city_slug, is_active, sort_order
) VALUES
  ('Argentina', 'AR', 'Buenos Aires', 'buenos-aires', true, 10),
  ('Argentina', 'AR', 'Córdoba', 'cordoba-ar', true, 20),
  ('Argentina', 'AR', 'Rosario', 'rosario-ar', true, 30),
  ('Argentina', 'AR', 'Mendoza', 'mendoza-ar', true, 40),
  ('Mexico', 'MX', 'Ciudad de México', 'cdmx', true, 10),
  ('Mexico', 'MX', 'Guadalajara', 'guadalajara-mx', true, 20),
  ('Mexico', 'MX', 'Monterrey', 'monterrey-mx', true, 30),
  ('Mexico', 'MX', 'Cancún', 'cancun', true, 40),
  ('United States', 'US', 'Miami', 'miami', true, 10),
  ('United States', 'US', 'New York', 'new-york', true, 20),
  ('United States', 'US', 'Houston', 'houston', true, 30),
  ('United States', 'US', 'Los Angeles', 'los-angeles', true, 40)
ON CONFLICT (city_slug) DO NOTHING;
