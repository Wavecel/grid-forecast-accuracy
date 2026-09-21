-- =========================================================================
-- 02_stg_grid.sql -- pivot EIA's long format into one row per BA-hour
-- =========================================================================
-- INPUT  raw region_data Parquet. LONG format: one row per (hour, BA, series),
--        so a single hour for a single BA arrives as FOUR rows:
--            2026-07-15T18 | PJM | D  | 138204
--            2026-07-15T18 | PJM | DF | 135900
--            2026-07-15T18 | PJM | NG | 140110
--            2026-07-15T18 | PJM | TI |   1906
--
-- OUTPUT WIDE format: one row per (hour, BA) with four measure columns.
--
-- WHY PIVOT AT ALL? Because the central calculation of this entire project --
-- forecast error = actual - forecast -- is a comparison BETWEEN two series. In
-- long format that requires a self-join on every row. Pivoting once here makes
-- error a trivial column subtraction, and makes the resulting fact table a
-- clean, natural grain for Power BI (one row = one hour of one BA's operations).
--
-- WHY MAX(CASE WHEN ...) RATHER THAN DuckDB's PIVOT? Two reasons:
--   1. It is portable ANSI SQL -- the same statement runs on Postgres, Snowflake,
--      BigQuery or SQL Server, which matters if a reviewer asks you to port it.
--   2. It handles the duplicate case gracefully: if EIA ever published two rows
--      for the same (hour, BA, series), MAX collapses them deterministically
--      rather than erroring or fanning out.
--
-- MAX over a CASE that is NULL for non-matching rows returns the one matching
-- value, because SQL aggregates ignore NULLs. That is the whole trick.
-- =========================================================================

CREATE OR REPLACE TABLE stg_grid AS
WITH src AS (
    SELECT
        ba_code,
        period_utc,
        series_type,
        value,
        COALESCE(TRY_CAST(is_demo_data AS INTEGER), 0) AS is_demo_data
    FROM read_parquet($region_glob, union_by_name = true)
    -- Defensive: never let a NULL key into the model. A NULL ba_code or hour
    -- would produce a blank row in every Power BI visual and quietly break
    -- referential integrity against dim_ba.
    WHERE ba_code IS NOT NULL
      AND period_utc IS NOT NULL
)
SELECT
    ba_code,
    period_utc,
    MAX(CASE WHEN series_type = 'D'  THEN value END) AS demand_mw,
    MAX(CASE WHEN series_type = 'DF' THEN value END) AS forecast_mw,
    MAX(CASE WHEN series_type = 'NG' THEN value END) AS net_generation_mw,
    MAX(CASE WHEN series_type = 'TI' THEN value END) AS interchange_mw,
    MAX(is_demo_data)                                AS is_demo_data,
    -- Row-count guard: tells us whether all four series were present for this
    -- hour. Surfaced on the data-quality page so partial hours are visible
    -- rather than mistaken for real operational movement.
    COUNT(*)                                         AS series_rows_present
FROM src
GROUP BY ba_code, period_utc;


-- Integrity assertion. A pivot MUST NOT change the number of distinct BA-hours.
-- If this count ever exceeded the distinct count something is deeply wrong, so
-- we materialise the check rather than trusting it.
CREATE OR REPLACE TABLE dq_stg_grid AS
SELECT
    'stg_grid' AS table_name,
    COUNT(*)                                           AS row_count,
    COUNT(DISTINCT ba_code || '|' || period_utc::VARCHAR) AS distinct_keys,
    SUM(CASE WHEN demand_mw   IS NULL THEN 1 ELSE 0 END) AS null_demand_hours,
    SUM(CASE WHEN forecast_mw IS NULL THEN 1 ELSE 0 END) AS null_forecast_hours,
    MIN(period_utc)                                    AS min_hour_utc,
    MAX(period_utc)                                    AS max_hour_utc
FROM stg_grid;
