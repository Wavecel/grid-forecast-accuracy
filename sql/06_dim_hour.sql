-- =========================================================================
-- 06_dim_hour.sql -- hour-of-day dimension (24 rows)
-- =========================================================================
-- GRAIN: one row per local hour of day, 0-23.
--
-- WHY BOTHER? You could slice on the raw integer `local_hour` in the fact table.
-- A dimension buys three things that integer cannot:
--   1. A readable axis label ('06:00' beats '6') without a DAX format string on
--      every visual.
--   2. Business groupings -- 'Morning ramp', 'Evening peak' -- that let you say
--      "our error concentrates in the evening ramp" instead of "in hours 17-21".
--      Operational users think in named windows, not integers.
--   3. A correct sort. Text labels sort alphabetically ('10:00' before '6:00')
--      unless an integer sort column exists. That column is `hour_of_day`.
--
-- THE PERIOD BOUNDARIES are the standard North American utility load-shape
-- windows. They are approximations and are declared here so they can be argued
-- with -- which is the point of putting assumptions in code.
-- =========================================================================

CREATE OR REPLACE TABLE dim_hour AS
SELECT
    h                                             AS hour_of_day,
    printf('%02d:00', h)                          AS hour_label,
    printf('%02d:00-%02d:59', h, h)               AS hour_range_label,
    CASE
        WHEN h BETWEEN 0  AND 5  THEN 'Overnight'
        WHEN h BETWEEN 6  AND 9  THEN 'Morning ramp'
        WHEN h BETWEEN 10 AND 15 THEN 'Midday'
        WHEN h BETWEEN 16 AND 20 THEN 'Evening peak'
        ELSE 'Late evening'
    END                                           AS day_period,
    -- Explicit sort key so 'Overnight' does not sort before 'Evening peak'.
    CASE
        WHEN h BETWEEN 0  AND 5  THEN 1
        WHEN h BETWEEN 6  AND 9  THEN 2
        WHEN h BETWEEN 10 AND 15 THEN 3
        WHEN h BETWEEN 16 AND 20 THEN 4
        ELSE 5
    END                                           AS day_period_sort,
    -- The window in which system peaks and scarcity events almost always occur.
    CASE WHEN h BETWEEN 14 AND 20 THEN 1 ELSE 0 END AS is_system_peak_window,
    -- The solar-set ramp window. CAISO's steepest ramps live here, and it is
    -- where an evening forecast miss is most expensive.
    CASE WHEN h BETWEEN 17 AND 20 THEN 1 ELSE 0 END AS is_solar_ramp_window,
    -- Overnight minimum load window, useful for baseload / minimum-generation
    -- analysis and as the natural comparison point for peak-to-trough spread.
    CASE WHEN h BETWEEN 2 AND 5 THEN 1 ELSE 0 END   AS is_overnight_trough_window
FROM (SELECT UNNEST(generate_series(0, 23)) AS h)
ORDER BY hour_of_day;
