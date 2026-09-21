-- =========================================================================
-- 09_dq_checks.sql -- data quality assertions, materialised as a table
-- =========================================================================
-- WHY MATERIALISE CHECKS INSTEAD OF PRINTING THEM
--
-- A check that only prints to a console log is a check nobody reads. By writing
-- results to a table that ships with the curated data, three things become true:
--   1. transform.py can fail the build on any severity='FAIL' row, so a broken
--      refresh stops before it reaches the dashboard.
--   2. Power BI can load dq_checks as a table and display data health ON the
--      dashboard. Showing your own data quality is a trust signal that almost
--      no portfolio project has, and it pre-empts the interviewer's question
--      "how would you know if this data were wrong?"
--   3. The results are versioned in git, so degradation over time is visible.
--
-- SEVERITY LEVELS
--   FAIL  breaks a modelling invariant -- the build must stop.
--   WARN  real-world messiness we tolerate but must disclose.
--   INFO  descriptive context, always passes.
-- =========================================================================

-- NOTE ON STRUCTURE: the UNION ALL block is wrapped in a subquery because SQL
-- does not allow ORDER BY on an expression that is not in the SELECT list of a
-- set operation. Wrapping is the portable fix and it keeps the ordering logic
-- (FAIL first, then WARN) next to the data it orders.
CREATE OR REPLACE TABLE dq_checks AS
SELECT * FROM (

-- ---- 1. FACT GRAIN UNIQUENESS -------------------------------------------
-- The defining invariant: one row per BA per hour. If this fails, every
-- aggregate in the report is inflated by duplicate rows -- the most dangerous
-- silent failure in dimensional modelling, because totals look plausible.
SELECT
    'fact_grain_unique'                                            AS check_name,
    'FAIL'                                                         AS severity,
    CASE WHEN COUNT(*) = COUNT(DISTINCT ba_code || '|' || period_utc::VARCHAR)
         THEN 'PASS' ELSE 'FAIL' END                               AS result,
    COUNT(*)                                                       AS observed,
    COUNT(DISTINCT ba_code || '|' || period_utc::VARCHAR)          AS expected,
    'fact_load_hour must hold exactly one row per (ba_code, period_utc)' AS description
FROM fact_load_hour

UNION ALL
-- ---- 2. REFERENTIAL INTEGRITY: fact -> dim_ba ---------------------------
-- An orphan fact row shows up in Power BI as a blank category, which users
-- interpret as "missing data" and analysts waste an afternoon on.
SELECT
    'fact_ba_orphans', 'FAIL',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
    COUNT(*), 0,
    'every fact_load_hour.ba_code must exist in dim_ba'
FROM fact_load_hour f
LEFT JOIN dim_ba b ON b.ba_code = f.ba_code
WHERE b.ba_code IS NULL

UNION ALL
-- ---- 3. REFERENTIAL INTEGRITY: fact -> dim_date -------------------------
SELECT
    'fact_date_orphans', 'FAIL',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
    COUNT(*), 0,
    'every fact_load_hour.date_key must exist in dim_date'
FROM fact_load_hour f
LEFT JOIN dim_date d ON d.date_key = f.date_key
WHERE d.date_key IS NULL

UNION ALL
-- ---- 4. CALENDAR CONTIGUITY --------------------------------------------
-- A gap here silently corrupts every DAX time-intelligence measure. Because
-- those functions return a wrong number rather than an error, this check is the
-- only thing standing between you and a confidently incorrect YoY figure.
SELECT
    'date_dim_contiguous', 'FAIL',
    CASE WHEN COUNT(*) = DATE_DIFF('day', MIN(date_key), MAX(date_key)) + 1
         THEN 'PASS' ELSE 'FAIL' END,
    COUNT(*),
    DATE_DIFF('day', MIN(date_key), MAX(date_key)) + 1,
    'dim_date must contain every date between its min and max with no gaps'
FROM dim_date

UNION ALL
-- ---- 5. NO NEGATIVE DEMAND --------------------------------------------
-- Physically impossible for a BA-level demand series. A negative value means a
-- bad publication or a sign error on our side, and it would poison MAPE
-- (dividing by a negative denominator).
SELECT
    'demand_non_negative', 'FAIL',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
    COUNT(*), 0,
    'demand_mw must never be negative'
FROM fact_load_hour
WHERE demand_mw < 0

UNION ALL
-- ---- 6. ERROR ARITHMETIC CONSISTENCY ----------------------------------
-- Guards against a future edit breaking the sign convention. Cheap to run,
-- catastrophic to get wrong, and the kind of self-check reviewers respect.
SELECT
    'error_arithmetic_consistent', 'FAIL',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'FAIL' END,
    COUNT(*), 0,
    'forecast_error_mw must equal demand_mw - forecast_mw within rounding'
FROM fact_load_hour
WHERE forecast_error_mw IS NOT NULL
  AND ABS(forecast_error_mw - (demand_mw - forecast_mw)) > 0.5

UNION ALL
-- ---- 7. HOURLY COVERAGE PER BA-DAY ------------------------------------
-- WARN not FAIL: DST transition days legitimately have 23 or 25 hours, and the
-- current (partial) day is legitimately short. Encoding that nuance -- rather
-- than demanding a rigid 24 -- is what separates a real check from a naive one.
SELECT
    'ba_day_hour_coverage', 'WARN',
    CASE WHEN COUNT(*) = 0 THEN 'PASS' ELSE 'WARN' END,
    COUNT(*), 0,
    'complete past BA-days should hold 23-25 hours (DST-aware); count of violations'
FROM (
    SELECT ba_code, date_key, COUNT(*) AS hrs
    FROM fact_load_hour
    WHERE date_key < CURRENT_DATE
      AND date_key > (SELECT MIN(date_key) FROM fact_load_hour)
    GROUP BY ba_code, date_key
    HAVING COUNT(*) NOT BETWEEN 23 AND 25
)

UNION ALL
-- ---- 8. FORECAST AVAILABILITY -----------------------------------------
-- EIA does not publish a day-ahead forecast for every BA in every hour. We
-- tolerate this but must quantify it, because MAPE computed over a biased
-- subset of hours is not comparable across BAs.
SELECT
    'forecast_coverage', 'WARN',
    CASE WHEN SUM(missing_forecast_flag)::DOUBLE / NULLIF(COUNT(*), 0) < 0.05
         THEN 'PASS' ELSE 'WARN' END,
    SUM(missing_forecast_flag),
    CAST(COUNT(*) * 0.05 AS INTEGER),
    'fewer than 5% of gradeable hours should be missing a day-ahead forecast'
FROM fact_load_hour
WHERE row_kind = 'actual'

UNION ALL
-- ---- 9. WEATHER JOIN COVERAGE -----------------------------------------
SELECT
    'weather_join_coverage', 'WARN',
    CASE WHEN SUM(missing_weather_flag)::DOUBLE / NULLIF(COUNT(*), 0) < 0.02
         THEN 'PASS' ELSE 'WARN' END,
    SUM(missing_weather_flag),
    CAST(COUNT(*) * 0.02 AS INTEGER),
    'fewer than 2% of gradeable hours should be missing weather'
FROM fact_load_hour
WHERE row_kind = 'actual'

UNION ALL
-- ---- 10. MAPE PLAUSIBILITY -------------------------------------------
-- A domain sanity check, not a technical one. Published day-ahead MAPE for
-- large US BAs sits around 1.5-4%. A model-wide MAPE outside 0.2%-15% almost
-- certainly means a unit error, a timezone misalignment, or actual and forecast
-- accidentally swapped -- all of which produce plausible-looking charts.
-- Knowing the expected range of your own metric is what makes you an analyst
-- rather than a report builder.
SELECT
    'mape_plausible', 'WARN',
    CASE WHEN AVG(ape) BETWEEN 0.002 AND 0.15 THEN 'PASS' ELSE 'WARN' END,
    CAST(ROUND(AVG(ape) * 10000) AS INTEGER),
    CAST(ROUND(0.04 * 10000) AS INTEGER),
    'overall MAPE should fall in 0.2%-15% (value shown in basis points)'
FROM fact_load_hour
WHERE row_kind = 'actual' AND ape IS NOT NULL

UNION ALL
-- ---- 11. DEMO-DATA GUARD --------------------------------------------
-- Publication safety catch. Prevents the embarrassing outcome of shipping a
-- portfolio dashboard built on the synthetic fixture.
SELECT
    'no_demo_data', 'WARN',
    CASE WHEN SUM(is_demo_data) = 0 THEN 'PASS' ELSE 'WARN' END,
    SUM(is_demo_data), 0,
    'curated output must contain no synthetic rows before publication'
FROM fact_load_hour

UNION ALL
-- ---- 12. FRESHNESS ---------------------------------------------------
-- The KPI behind "can I trust what I am looking at right now?".
SELECT
    'data_freshness_hours', 'WARN',
    CASE WHEN DATE_DIFF('hour', MAX(period_utc), NOW()) <= 6 THEN 'PASS' ELSE 'WARN' END,
    DATE_DIFF('hour', MAX(period_utc), NOW()),
    6,
    'newest actual demand hour should be within 6 hours of now'
FROM fact_load_hour
WHERE row_kind = 'actual'

) AS all_checks
ORDER BY
    CASE severity WHEN 'FAIL' THEN 1 WHEN 'WARN' THEN 2 ELSE 3 END,
    result DESC,
    check_name;
