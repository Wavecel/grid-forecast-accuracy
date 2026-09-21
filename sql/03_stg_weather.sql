-- =========================================================================
-- 03_stg_weather.sql -- BA-level weather, de-duplicated across sources
-- =========================================================================
-- INPUT  raw weather_city Parquet: one row per (hour, BA, city, source).
-- OUTPUT one row per (hour, BA): population-weighted temperature + degree hours.
--
-- This repeats the blend that weather_client.blend_to_ba() does in pandas.
-- WHY DO IT TWICE? It is not duplication for its own sake -- it is the
-- separation of concerns the whole architecture rests on:
--     the Python blend exists so the DEMO generator has a weather signal to
--     drive synthetic load,
--     this SQL blend is the PRODUCTION path, because raw is the only thing we
--     trust and the transform must be reproducible from raw alone.
-- If they ever disagree, 09_dq_checks.sql catches it.
--
-- THE WEIGHTING SUBTLETY (worth being able to explain out loud)
-- We divide by SUM(weight) over cities that actually reported, not by the full
-- declared weight. If Fresno's series has a gap, a fixed denominator would drag
-- California's blended temperature toward zero and invent a cold snap that
-- never happened -- and then the model would "discover" a forecast miss caused
-- entirely by our own arithmetic. Renormalising on present weight is the
-- correct handling of partial data.
-- =========================================================================

CREATE OR REPLACE TABLE stg_weather AS
WITH src AS (
    SELECT
        ba_code,
        period_utc,
        city,
        city_weight,
        weather_source,
        temp_f,
        humidity_pct,
        wind_mph,
        cloud_pct,
        -- ERA5 reanalysis ('archive') is the higher-quality product, so where
        -- both sources cover an hour we prefer archive. Ranking by source name
        -- ascending does this for free because 'archive' < 'forecast'.
        ROW_NUMBER() OVER (
            PARTITION BY ba_code, period_utc, city
            ORDER BY weather_source ASC
        ) AS source_rank
    FROM read_parquet($weather_glob, union_by_name = true)
    WHERE ba_code IS NOT NULL
      AND period_utc IS NOT NULL
),
deduped AS (
    SELECT * FROM src WHERE source_rank = 1
),
weighted AS (
    SELECT
        ba_code,
        period_utc,
        -- Weight counted only where the measure is present.
        SUM(CASE WHEN temp_f IS NOT NULL THEN city_weight ELSE 0 END)                AS w_temp,
        SUM(CASE WHEN temp_f IS NOT NULL THEN temp_f * city_weight ELSE 0 END)        AS num_temp,
        SUM(CASE WHEN humidity_pct IS NOT NULL THEN city_weight ELSE 0 END)           AS w_hum,
        SUM(CASE WHEN humidity_pct IS NOT NULL THEN humidity_pct * city_weight ELSE 0 END) AS num_hum,
        SUM(CASE WHEN wind_mph IS NOT NULL THEN city_weight ELSE 0 END)               AS w_wind,
        SUM(CASE WHEN wind_mph IS NOT NULL THEN wind_mph * city_weight ELSE 0 END)    AS num_wind,
        SUM(CASE WHEN cloud_pct IS NOT NULL THEN city_weight ELSE 0 END)              AS w_cloud,
        SUM(CASE WHEN cloud_pct IS NOT NULL THEN cloud_pct * city_weight ELSE 0 END)  AS num_cloud,
        COUNT(DISTINCT city)                                                          AS cities_total,
        COUNT(DISTINCT CASE WHEN temp_f IS NOT NULL THEN city END)                    AS cities_reporting,
        -- Provenance: if any contributing city came from the live forecast model
        -- rather than reanalysis, say so. Honesty about provenance is cheap here
        -- and expensive to retrofit.
        CASE WHEN COUNT(DISTINCT weather_source) = 1 AND MIN(weather_source) = 'archive'
             THEN 'archive' ELSE 'mixed/forecast' END                                 AS weather_source
    FROM deduped
    GROUP BY ba_code, period_utc
)
SELECT
    ba_code,
    period_utc,
    ROUND(CASE WHEN w_temp  > 0 THEN num_temp  / w_temp  END, 2) AS temp_f,
    ROUND(CASE WHEN w_hum   > 0 THEN num_hum   / w_hum   END, 1) AS humidity_pct,
    ROUND(CASE WHEN w_wind  > 0 THEN num_wind  / w_wind  END, 1) AS wind_mph,
    ROUND(CASE WHEN w_cloud > 0 THEN num_cloud / w_cloud END, 1) AS cloud_pct,
    -- Degree hours: the US utility industry's standard weather-normalisation
    -- unit. Measured from a 65F base, the temperature at which neither heating
    -- nor cooling load is triggered. GREATEST(...,0) implements the "only count
    -- the side of the base you are on" rule.
    ROUND(GREATEST((CASE WHEN w_temp > 0 THEN num_temp / w_temp END) - $degree_base, 0), 2) AS cooling_degree_hours,
    ROUND(GREATEST($degree_base - (CASE WHEN w_temp > 0 THEN num_temp / w_temp END), 0), 2) AS heating_degree_hours,
    cities_reporting,
    cities_total,
    ROUND(cities_reporting::DOUBLE / NULLIF(cities_total, 0), 3) AS weather_coverage,
    weather_source
FROM weighted;
