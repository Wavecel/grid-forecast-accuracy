-- =========================================================================
-- 04_fact_load_hour.sql -- THE FACT TABLE
-- =========================================================================
-- GRAIN: exactly one row per (balancing authority, UTC hour).
--
-- Declaring the grain in one sentence, before writing any SQL, is the single
-- most valuable habit in dimensional modelling. Everything else follows from it:
-- if a measure cannot be expressed at this grain, it belongs in a different
-- table; if a join could produce two rows for one BA-hour, it is a bug.
--
-- WHY FULL OUTER JOIN BETWEEN GRID AND WEATHER
-- ---------------------------------------------
-- An INNER JOIN would be the obvious choice and it would silently destroy the
-- most valuable page of the dashboard. Here is why:
--     * EIA publishes actual demand (D) up to ~1-2 hours ago.
--     * EIA publishes day-ahead forecast (DF) ~24-36 hours ahead.
--     * Open-Meteo publishes weather ~72 hours ahead.
-- Those three horizons are different. An INNER JOIN keeps only the overlap and
-- throws away the forward weather -- which is exactly the data the "peak risk
-- in the next 72 hours" early-warning page needs.
--
-- So we FULL OUTER JOIN and label each row's nature in `row_kind`:
--     'actual'        actual demand exists -> gradeable, feeds all error KPIs
--     'forecast_only' forecast exists, actual not yet published -> pending
--     'weather_only'  beyond the forecast horizon -> forward outlook only
-- Every DAX error measure filters to row_kind = 'actual'. That single discipline
-- keeps future rows from diluting MAPE, which is the trap this design avoids.
--
-- SIGN CONVENTION (state this explicitly or you will confuse yourself)
-- ---------------------------------------------------------------------
--     forecast_error_mw = actual - forecast
--     POSITIVE => actual came in ABOVE forecast => the BA UNDER-forecast
--                 => it was SHORT and had to buy power at (usually higher) spot
--     NEGATIVE => actual came in BELOW forecast => the BA OVER-forecast
--                 => it was LONG and had to sell surplus back, often at a loss
-- Both directions cost money, which is why we track signed bias separately from
-- absolute error. Absolute error tells you how noisy the forecast is; signed
-- bias tells you whether it is systematically wrong -- and only the second one
-- is fixable by the forecasting team.
-- =========================================================================

CREATE OR REPLACE TABLE fact_load_hour AS
WITH spine AS (
    -- FULL OUTER so neither horizon truncates the other.
    SELECT
        COALESCE(g.ba_code, w.ba_code)       AS ba_code,
        COALESCE(g.period_utc, w.period_utc) AS period_utc,
        g.demand_mw,
        g.forecast_mw,
        g.net_generation_mw,
        g.interchange_mw,
        COALESCE(g.is_demo_data, 0)          AS is_demo_data,
        g.series_rows_present,
        w.temp_f,
        w.humidity_pct,
        w.wind_mph,
        w.cloud_pct,
        w.cooling_degree_hours,
        w.heating_degree_hours,
        w.weather_coverage,
        w.weather_source
    FROM stg_grid g
    FULL OUTER JOIN stg_weather w
        ON  w.ba_code    = g.ba_code
        AND w.period_utc = g.period_utc
),
localised AS (
    SELECT
        s.*,
        b.timezone,
        -- The one and only place UTC becomes local time. ICU handles DST, so
        -- 'America/New_York' is -4 in July and -5 in January automatically.
        -- Doing this by hand with a fixed offset is how hour-of-day analysis
        -- ends up wrong for half the year.
        (s.period_utc AT TIME ZONE b.timezone)                  AS local_datetime,
        CAST((s.period_utc AT TIME ZONE b.timezone) AS DATE)    AS date_key,
        EXTRACT('hour' FROM (s.period_utc AT TIME ZONE b.timezone))::INTEGER AS local_hour
    FROM spine s
    -- INNER JOIN here is correct and intentional: a row for a BA that is not in
    -- our dimension is meaningless and must not enter the model. This enforces
    -- referential integrity at build time rather than discovering a blank
    -- category in a Power BI visual later.
    INNER JOIN dim_ba b ON b.ba_code = s.ba_code
),
classified AS (
    SELECT
        l.*,
        CASE
            WHEN l.demand_mw   IS NOT NULL THEN 'actual'
            WHEN l.forecast_mw IS NOT NULL THEN 'forecast_only'
            ELSE 'weather_only'
        END AS row_kind,
        -- Holiday and weekend classification. Load on a public holiday behaves
        -- like a Sunday regardless of which weekday it falls on -- forecasters
        -- know this, and holidays are still where forecasts miss most, because
        -- there are only ~10 examples per year to learn from.
        CASE
            WHEN h.holiday_date IS NOT NULL THEN 'Holiday'
            WHEN EXTRACT('dow' FROM l.local_datetime) IN (0, 6) THEN 'Weekend'
            ELSE 'Weekday'
        END AS day_type,
        h.holiday_name
    FROM localised l
    LEFT JOIN seed_holiday h ON h.holiday_date = l.date_key
),
derived AS (
    SELECT
        c.*,
        -- ---------- core error arithmetic ----------
        CASE WHEN c.demand_mw IS NOT NULL AND c.forecast_mw IS NOT NULL
             THEN c.demand_mw - c.forecast_mw END                        AS forecast_error_mw,
        CASE WHEN c.demand_mw IS NOT NULL AND c.forecast_mw IS NOT NULL
             THEN ABS(c.demand_mw - c.forecast_mw) END                   AS abs_error_mw,
        -- NULLIF guards division by zero. A zero or negative demand reading is
        -- itself bad data, so we return NULL rather than a nonsense percentage.
        CASE WHEN c.demand_mw > 0 AND c.forecast_mw IS NOT NULL
             THEN ABS(c.demand_mw - c.forecast_mw) / c.demand_mw END     AS ape,
        CASE WHEN c.demand_mw > 0 AND c.forecast_mw IS NOT NULL
             THEN (c.demand_mw - c.forecast_mw) / c.demand_mw END        AS signed_pe,

        -- ---------- hour-over-hour dynamics ----------
        -- Ramp is the operational constraint that actually binds: a grid can
        -- serve a high load, but it cannot always CHANGE output fast enough.
        -- CAISO's evening solar ramp is the textbook case.
        c.demand_mw - LAG(c.demand_mw) OVER w_hour                       AS ramp_mw,
        c.temp_f    - LAG(c.temp_f)    OVER w_hour                       AS temp_change_1h,

        -- ---------- daily peak identification ----------
        MAX(c.demand_mw)   OVER w_day                                    AS daily_peak_mw,
        MAX(c.forecast_mw) OVER w_day                                    AS daily_peak_forecast_mw,

        -- ---------- rolling behavioural baseline ----------
        -- 720 hours = 30 days. THE CRITICAL DETAIL is the frame:
        --     ROWS BETWEEN 720 PRECEDING AND 1 PRECEDING
        -- The current hour is deliberately EXCLUDED from its own baseline. If it
        -- were included, a genuinely extreme hour would inflate the mean and
        -- standard deviation it is being compared against, and would therefore
        -- partly hide itself. That is data leakage, and it is the difference
        -- between an anomaly detector that works and one that flatters itself.
        AVG(c.demand_mw) OVER w_roll30                                   AS demand_roll30_mean,
        AVG(CASE WHEN c.demand_mw > 0 AND c.forecast_mw IS NOT NULL
                 THEN ABS(c.demand_mw - c.forecast_mw) / c.demand_mw END)
            OVER w_roll30                                                AS ape_roll30_mean,
        STDDEV_SAMP(CASE WHEN c.demand_mw > 0 AND c.forecast_mw IS NOT NULL
                         THEN ABS(c.demand_mw - c.forecast_mw) / c.demand_mw END)
            OVER w_roll30                                                AS ape_roll30_std,
        COUNT(c.demand_mw) OVER w_roll30                                 AS roll30_sample_hours
    FROM classified c
    WINDOW
        w_hour AS (PARTITION BY c.ba_code ORDER BY c.period_utc),
        w_day  AS (PARTITION BY c.ba_code, c.date_key),
        w_roll30 AS (
            PARTITION BY c.ba_code
            ORDER BY c.period_utc
            ROWS BETWEEN 720 PRECEDING AND 1 PRECEDING
        )
)
SELECT
    -- ---------- keys ----------
    ba_code,
    period_utc,
    local_datetime,
    date_key,
    local_hour,

    -- ---------- classification ----------
    row_kind,
    day_type,
    holiday_name,
    is_demo_data,

    -- ---------- measures: operations ----------
    ROUND(demand_mw, 1)                                   AS demand_mw,
    ROUND(forecast_mw, 1)                                 AS forecast_mw,
    ROUND(net_generation_mw, 1)                           AS net_generation_mw,
    ROUND(interchange_mw, 1)                              AS interchange_mw,
    ROUND(ramp_mw, 1)                                     AS ramp_mw,

    -- ---------- measures: forecast quality ----------
    ROUND(forecast_error_mw, 1)                           AS forecast_error_mw,
    ROUND(abs_error_mw, 1)                                AS abs_error_mw,
    ROUND(ape, 6)                                         AS ape,
    ROUND(signed_pe, 6)                                   AS signed_pe,

    -- ---------- measures: weather ----------
    temp_f,
    humidity_pct,
    wind_mph,
    cloud_pct,
    cooling_degree_hours,
    heating_degree_hours,
    ROUND(temp_change_1h, 2)                              AS temp_change_1h,

    -- ---------- flags for the alert page ----------
    -- Peak hour of the local day for this BA. Peak-hour accuracy matters far
    -- more than average accuracy: capacity charges, reserve procurement and
    -- scarcity pricing are all settled at the peak.
    CASE WHEN demand_mw IS NOT NULL AND demand_mw = daily_peak_mw
         THEN 1 ELSE 0 END                                AS is_daily_peak_hour,
    CASE WHEN ape > $material_miss THEN 1 ELSE 0 END       AS is_material_miss,

    -- Robust z-score of this hour's APE against its own trailing 30 days.
    -- Requiring 200+ sample hours prevents the first month of history from
    -- generating a wall of false alarms off a thin baseline -- a courtesy your
    -- users will never notice, and would resent the absence of.
    CASE
        WHEN roll30_sample_hours >= 200
         AND ape_roll30_std > 0
         AND ape IS NOT NULL
        THEN ROUND((ape - ape_roll30_mean) / ape_roll30_std, 2)
    END                                                   AS ape_zscore,
    ROUND(ape_roll30_mean, 6)                             AS ape_roll30_mean,
    ROUND(demand_roll30_mean, 1)                          AS demand_roll30_mean,
    roll30_sample_hours,

    -- ---------- data quality ----------
    weather_coverage,
    weather_source,
    series_rows_present,
    CASE WHEN temp_f IS NULL THEN 1 ELSE 0 END            AS missing_weather_flag,
    CASE WHEN row_kind = 'actual' AND forecast_mw IS NULL
         THEN 1 ELSE 0 END                                AS missing_forecast_flag
FROM derived
ORDER BY ba_code, period_utc;
