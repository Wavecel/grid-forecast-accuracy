-- =========================================================================
-- 01_dim_ba.sql -- Balancing Authority dimension
-- =========================================================================
-- GRAIN: one row per balancing authority. 6 rows.
--
-- WHY A DIMENSION TABLE FOR ONLY SIX ROWS?
-- Because grain, not size, decides what is a dimension. dim_ba holds the
-- descriptive attributes we slice by (market, timezone, peak season, region)
-- and the fact table holds only the measurable events. If we denormalised
-- 'ISO New England' into 110,000 fact rows we would:
--   * inflate the model,
--   * make renaming a BA a 110,000-row update instead of a 1-row update,
--   * and lose the ability to show a BA with zero rows (a data-gap finding).
--
-- The seed table `seed_ba` is registered by transform.py directly from
-- src/config.py, so Python config is the single source of truth and this SQL
-- can never disagree with the ingest code.
-- =========================================================================

CREATE OR REPLACE TABLE dim_ba AS
SELECT
    ba_code                                        AS ba_code,
    ba_name                                        AS ba_name,
    market                                         AS market,
    timezone                                       AS timezone,
    peak_season                                    AS peak_season,
    city_count                                     AS weather_city_count,
    weather_cities                                 AS weather_cities,
    -- A display label that sorts sensibly and reads well on a visual axis.
    ba_name || ' (' || ba_code || ')'              AS ba_label,
    -- Rough size band, used for conditional formatting and for the honest point
    -- that small BAs are intrinsically harder to forecast (less load diversity
    -- means individual behaviour is not averaged away).
    CASE
        WHEN ba_code IN ('PJM', 'MISO')  THEN 'Very large (>100 GW peak)'
        WHEN ba_code IN ('ERCO', 'CISO') THEN 'Large (40-90 GW peak)'
        ELSE 'Mid-size (<40 GW peak)'
    END                                            AS size_band,
    CASE
        WHEN ba_code IN ('ISNE', 'NYIS')          THEN 'Northeast'
        WHEN ba_code = 'PJM'                      THEN 'Mid-Atlantic / Midwest'
        WHEN ba_code = 'MISO'                     THEN 'Midcontinent'
        WHEN ba_code = 'ERCO'                     THEN 'Texas'
        WHEN ba_code = 'CISO'                     THEN 'West'
    END                                            AS region
FROM seed_ba
ORDER BY ba_code;
