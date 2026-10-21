-- migrate:up
-- Grid regions (spec 14/15/17 "Utility, balancing authority, ISO/RTO"): interconnections, ISO/RTO
-- footprints, balancing authority areas, electric retail service territories, transmission system
-- operators and reliability (NERC) regions. Identity only: id, kind, name, country, parent and the
-- published boundary. `meta` records where each row and its boundary came from (source, URL,
-- document id, as-of date) and coverage notes; values shown on screens are facts on the campus,
-- building, site or plant (fields iso, balancing_authority, utility, interconnection), written by the
-- spatial join with the boundary document as their source.
--
-- ids: 'interconnection:<slug>', 'iso:<short>', 'ba:<EIA code>' (or 'ba:eia<EIA id>' / 'ba:<slug>'
-- when EIA publishes no code), 'utility:<EIA utility number>', 'tso:<slug>', 'nerc:<entity>-<subregion>'.
CREATE TABLE grid_region (
  id          text PRIMARY KEY,
  kind        text NOT NULL CHECK (kind IN ('interconnection', 'iso_rto', 'balancing_authority', 'utility', 'tso',
                                            'reliability_region')),
  name        text NOT NULL,
  country     text,                             -- ISO 3166-1 alpha-2; null when the region spans countries
  parent_id   text REFERENCES grid_region(id) ON DELETE SET NULL,
  geom        geometry(MultiPolygon, 4326),     -- null for regions registered without a boundary
  meta        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CHECK (parent_id IS DISTINCT FROM id)
);
CREATE INDEX grid_region_geom ON grid_region USING gist (geom);
CREATE INDEX grid_region_kind ON grid_region (kind);
CREATE INDEX grid_region_parent ON grid_region (parent_id);

-- migrate:down
DROP TABLE IF EXISTS grid_region;
