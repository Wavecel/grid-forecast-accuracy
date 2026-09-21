-- =========================================================================
-- 07_fact_fuel_mix.sql -- second fact table, DIFFERENT grain
-- =========================================================================
-- GRAIN: one row per (balancing authority, UTC hour, fuel type).
--
-- WHY A SEPARATE TABLE INSTEAD OF MORE COLUMNS ON fact_load_hour?
-- Because the grain is different. fact_load_hour is one row per BA-hour;
-- fuel mix is one row per BA-hour-FUEL. Cramming fuel into the load fact would
-- force one of two bad outcomes:
--     (a) pivot fuel into ~8 columns, which breaks the moment EIA adds a fuel
--         type and cannot be sliced by a Fuel dimension, or
--     (b) fan the load fact out 8x, which would multiply every demand figure by
--         8 in any total -- the classic fan-trap that produces a dashboard where
--         demand is eight times reality and nobody notices for a month.
--
-- Two facts at different grains, both joined to the SHARED dim_ba, dim_date and
-- dim_hour, is textbook star schema. Those shared dimensions are called
-- CONFORMED dimensions, and being able to use that term correctly -- and explain
-- that it means one BA slicer filters both facts consistently -- is a genuine
-- interview differentiator.
-- =========================================================================

CREATE OR REPLACE TABLE dim_fuel AS
SELECT * FROM (VALUES
    ('SUN', 'Solar',            'Renewable',     1, 1, 1),
    ('WND', 'Wind',             'Renewable',     1, 1, 2),
    ('WAT', 'Hydro',            'Renewable',     1, 0, 3),
    ('NUC', 'Nuclear',          'Zero-carbon',   0, 0, 4),
    ('NG',  'Natural gas',      'Fossil',        0, 0, 5),
    ('COL', 'Coal',             'Fossil',        0, 0, 6),
    ('OIL', 'Petroleum',        'Fossil',        0, 0, 7),
    ('OTH', 'Other',            'Other',         0, 0, 8)
) AS t(fuel_code, fuel_name, fuel_category, is_renewable, is_variable, fuel_sort);
-- is_variable marks the intermittent, non-dispatchable sources. This is the
-- column that connects fuel mix back to the core thesis: as variable generation
-- share rises, NET load (demand minus wind and solar) becomes harder to
-- forecast than gross demand, because you are now forecasting two weather
-- processes and subtracting them.

CREATE OR REPLACE TABLE fact_fuel_mix AS
WITH src AS (
    SELECT
        ba_code,
        period_utc,
        COALESCE(fuel_code, 'OTH') AS fuel_code,
        value                      AS generation_mw
    FROM read_parquet($fuel_glob, union_by_name = true)
    WHERE ba_code IS NOT NULL
      AND period_utc IS NOT NULL
),
agg AS (
    -- Collapse any duplicate publication of the same key deterministically.
    SELECT ba_code, period_utc, fuel_code, MAX(generation_mw) AS generation_mw
    FROM src
    GROUP BY 1, 2, 3
),
with_total AS (
    SELECT
        a.*,
        -- Hourly total across all fuels for this BA, so share can be computed at
        -- row level. Computing share in SQL here (rather than only in DAX) is a
        -- deliberate choice: it makes the row independently interpretable and
        -- lets the website read shares without a semantic model.
        SUM(GREATEST(a.generation_mw, 0)) OVER (PARTITION BY a.ba_code, a.period_utc) AS total_gen_mw
    FROM agg a
)
SELECT
    w.ba_code,
    w.period_utc,
    CAST((w.period_utc AT TIME ZONE b.timezone) AS DATE)                  AS date_key,
    EXTRACT('hour' FROM (w.period_utc AT TIME ZONE b.timezone))::INTEGER  AS local_hour,
    w.fuel_code,
    ROUND(w.generation_mw, 1)                                             AS generation_mw,
    ROUND(w.total_gen_mw, 1)                                              AS total_generation_mw,
    ROUND(GREATEST(w.generation_mw, 0) / NULLIF(w.total_gen_mw, 0), 5)    AS generation_share
FROM with_total w
INNER JOIN dim_ba b ON b.ba_code = w.ba_code
ORDER BY w.ba_code, w.period_utc, w.fuel_code;
