-- migrate:up
-- US county boundaries (Census Bureau cartographic boundary files, collector census_counties). Identity
-- only: the published county polygon, so the grid spatial join (grid_join) can place a campus, site or
-- plant that has a county and state but no point. `meta` records the file it came from (source URL,
-- document id, vintage). Values derived from a county polygon are facts on the subject, with that
-- polygon named in their quote_loc.
CREATE TABLE us_county (
  geoid       text PRIMARY KEY,                 -- state FIPS + county FIPS, e.g. '48495' (Winkler County, TX)
  statefp     text NOT NULL,
  countyfp    text NOT NULL,
  stusps      text NOT NULL,                    -- USPS state code
  name        text NOT NULL,                    -- 'Winkler', 'Doña Ana'
  namelsad    text NOT NULL,                    -- 'Winkler County', 'Richland Parish', 'Manassas city'
  geom        geometry(MultiPolygon, 4326) NOT NULL,
  meta        jsonb NOT NULL DEFAULT '{}',
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (geoid = statefp || countyfp)
);
CREATE INDEX us_county_geom ON us_county USING gist (geom);
CREATE INDEX us_county_state ON us_county (stusps);

-- migrate:down
DROP TABLE IF EXISTS us_county;
