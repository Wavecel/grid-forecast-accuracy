-- =========================================================================
-- 05_dim_date.sql -- calendar dimension
-- =========================================================================
-- GRAIN: one row per calendar date, CONTIGUOUS with no gaps.
--
-- WHY CONTIGUITY IS NON-NEGOTIABLE
-- Power BI's time-intelligence functions (SAMEPERIODLASTYEAR, DATESINPERIOD,
-- TOTALYTD, DATEADD) walk the date table row by row. A single missing date --
-- say a day when the API returned nothing -- makes those functions return
-- silently wrong answers rather than errors. Building the calendar from a
-- generated series instead of from SELECT DISTINCT on the fact table is what
-- guarantees no gaps.
--
-- We also extend the calendar forward past the last actual hour, because the
-- forward weather outlook occupies dates that have no demand yet. If the
-- calendar stopped at "today", the peak-risk page would have unmatched rows.
--
-- SEASON DEFINITION: meteorological seasons (Dec-Feb winter etc.), not
-- astronomical, because that is what load forecasters use -- electricity demand
-- responds to weather, and weather does not wait for the solstice.
-- =========================================================================

CREATE OR REPLACE TABLE dim_date AS
WITH bounds AS (
    SELECT
        MIN(date_key)                    AS min_date,
        -- Pad forward so forward-looking weather dates always have a calendar
        -- row, and pad to a month end so month-to-date visuals behave.
        MAX(date_key) + INTERVAL 7 DAY   AS max_date
    FROM fact_load_hour
),
series AS (
    SELECT CAST(UNNEST(generate_series(
        (SELECT min_date FROM bounds),
        (SELECT CAST(max_date AS DATE) FROM bounds),
        INTERVAL 1 DAY
    )) AS DATE) AS date_key
)
SELECT
    s.date_key,
    EXTRACT('year'    FROM s.date_key)::INTEGER                       AS calendar_year,
    EXTRACT('quarter' FROM s.date_key)::INTEGER                       AS calendar_quarter,
    'Q' || EXTRACT('quarter' FROM s.date_key)::VARCHAR                AS quarter_label,
    EXTRACT('month'   FROM s.date_key)::INTEGER                       AS month_number,
    monthname(s.date_key)                                             AS month_name,
    LEFT(monthname(s.date_key), 3)                                    AS month_short,
    -- 'year_month' as an INTEGER (202607) sorts correctly with zero effort and
    -- is the column you set as the Sort By column for month_name in Power BI.
    (EXTRACT('year' FROM s.date_key) * 100
        + EXTRACT('month' FROM s.date_key))::INTEGER                  AS year_month,
    strftime(s.date_key, '%Y-%m')                                     AS year_month_label,
    EXTRACT('day'       FROM s.date_key)::INTEGER                     AS day_of_month,
    EXTRACT('dayofyear' FROM s.date_key)::INTEGER                     AS day_of_year,
    EXTRACT('week'      FROM s.date_key)::INTEGER                     AS iso_week,
    -- DuckDB dow: 0 = Sunday. Convert to an ISO-friendly 1 = Monday ordering so
    -- weekday axes read Mon..Sun like every operational report expects.
    CASE WHEN EXTRACT('dow' FROM s.date_key) = 0
         THEN 7 ELSE EXTRACT('dow' FROM s.date_key) END::INTEGER      AS iso_day_of_week,
    dayname(s.date_key)                                               AS day_name,
    LEFT(dayname(s.date_key), 3)                                      AS day_short,
    CASE WHEN EXTRACT('dow' FROM s.date_key) IN (0, 6) THEN 1 ELSE 0 END AS is_weekend,
    CASE WHEN h.holiday_date IS NOT NULL THEN 1 ELSE 0 END            AS is_holiday,
    h.holiday_name,
    CASE
        WHEN h.holiday_date IS NOT NULL                     THEN 'Holiday'
        WHEN EXTRACT('dow' FROM s.date_key) IN (0, 6)       THEN 'Weekend'
        ELSE 'Weekday'
    END                                                               AS day_type,
    -- Meteorological season, used to separate cooling-driven from
    -- heating-driven forecast error.
    CASE
        WHEN EXTRACT('month' FROM s.date_key) IN (12, 1, 2) THEN 'Winter'
        WHEN EXTRACT('month' FROM s.date_key) IN (3, 4, 5)  THEN 'Spring'
        WHEN EXTRACT('month' FROM s.date_key) IN (6, 7, 8)  THEN 'Summer'
        ELSE 'Autumn'
    END                                                               AS season,
    -- Summer peak season is when capacity charges and scarcity risk concentrate.
    CASE WHEN EXTRACT('month' FROM s.date_key) BETWEEN 6 AND 9
         THEN 1 ELSE 0 END                                            AS is_summer_peak_season,

    -- Relative-date helpers, recomputed on every refresh.
    -- CAVEAT worth knowing: these are relative to REFRESH time, not report-view
    -- time. They are convenient for a "Last 7 days" slicer but the DAX measures
    -- deliberately use date arithmetic instead, so the report stays correct even
    -- if a refresh is missed.
    DATE_DIFF('day', s.date_key, CURRENT_DATE)::INTEGER               AS days_ago,
    CASE WHEN s.date_key = CURRENT_DATE THEN 1 ELSE 0 END             AS is_today,
    CASE WHEN s.date_key = CURRENT_DATE - 1 THEN 1 ELSE 0 END         AS is_yesterday,
    CASE WHEN s.date_key >  CURRENT_DATE THEN 1 ELSE 0 END            AS is_future,
    CASE WHEN s.date_key BETWEEN CURRENT_DATE - 6  AND CURRENT_DATE THEN 1 ELSE 0 END AS is_last_7_days,
    CASE WHEN s.date_key BETWEEN CURRENT_DATE - 29 AND CURRENT_DATE THEN 1 ELSE 0 END AS is_last_30_days
FROM series s
LEFT JOIN seed_holiday h ON h.holiday_date = s.date_key
ORDER BY s.date_key;
