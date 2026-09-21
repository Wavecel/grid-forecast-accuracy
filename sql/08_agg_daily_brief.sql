-- =========================================================================
-- 08_agg_daily_brief.sql -- pre-aggregated daily summary + generated narrative
-- =========================================================================
-- GRAIN: one row per (balancing authority, local date).
--
-- WHY PRE-AGGREGATE WHEN DAX COULD DO THIS?
-- Normally you should NOT pre-aggregate: DAX measures over the hourly fact are
-- more flexible and respond to every slicer. This table exists for two specific
-- reasons that justify the exception:
--   1. THE WEBSITE. The public web dashboard has no DAX engine. It reads JSON.
--      Shipping 110,000 hourly rows to a browser is wasteful; shipping ~4,500
--      daily rows is instant. This table IS the website's data source.
--   2. THE NARRATIVE. A generated sentence ("MAPE rose to 3.4%, driven by the
--      evening peak") is a row-level string, not an aggregation. It cannot be
--      produced by a DAX measure that has to respond to arbitrary filters, and
--      it is the difference between a dashboard that reports numbers and one
--      that tells the user what happened.
--
-- BASELINE CHOICE: we compare each day against the trailing 28 days OF THE SAME
-- DAY TYPE. Comparing a Monday to "yesterday" (a Sunday) would attribute a
-- normal weekday load rise to a forecast failure. Comparing like to like -- 4
-- prior weekdays, or 4 prior weekend days -- is what makes the day-over-day
-- signal trustworthy. This is the single most important analytical decision in
-- the file.
-- =========================================================================

CREATE OR REPLACE TABLE agg_daily_brief AS
WITH daily AS (
    SELECT
        f.ba_code,
        f.date_key,
        ANY_VALUE(f.day_type)                                     AS day_type,
        ANY_VALUE(f.holiday_name)                                 AS holiday_name,
        COUNT(*) FILTER (WHERE f.row_kind = 'actual')             AS hours_with_actual,
        -- 24 expected hours; fewer means a data gap, which we surface rather
        -- than average away.
        COUNT(*)                                                  AS hours_total,

        -- ---------- load ----------
        ROUND(AVG(f.demand_mw), 1)                                AS avg_demand_mw,
        ROUND(MAX(f.demand_mw), 1)                                AS peak_demand_mw,
        ROUND(MIN(f.demand_mw), 1)                                AS min_demand_mw,
        ROUND(SUM(f.demand_mw), 1)                                AS total_demand_mwh,
        ROUND(MAX(f.demand_mw) - MIN(f.demand_mw), 1)             AS peak_to_trough_mw,
        ROUND(MAX(ABS(f.ramp_mw)), 1)                             AS max_abs_ramp_mw,

        -- ---------- forecast quality ----------
        -- MAPE: the mean of hourly absolute percentage errors. Note this is the
        -- mean of RATIOS, not the ratio of sums -- those differ, and the mean of
        -- ratios is the industry convention because it weights every hour
        -- equally rather than letting peak hours dominate.
        ROUND(AVG(f.ape), 6)                                      AS mape,
        -- Signed bias: the direction of systematic error. Near zero means the
        -- forecast is noisy but unbiased; persistently negative means it
        -- habitually over-forecasts.
        ROUND(AVG(f.signed_pe), 6)                                AS bias_pct,
        ROUND(AVG(f.forecast_error_mw), 1)                        AS avg_error_mw,
        ROUND(SUM(f.abs_error_mw), 1)                             AS total_abs_error_mwh,
        ROUND(MAX(f.ape), 6)                                      AS worst_hour_ape,
        COUNT(*) FILTER (WHERE f.is_material_miss = 1)            AS material_miss_hours,

        -- Peak-hour accuracy, isolated. This is the number a capacity planner
        -- actually cares about: being right on average is no comfort if you were
        -- wrong at the moment the system was most stressed.
        ROUND(AVG(f.ape) FILTER (WHERE f.is_daily_peak_hour = 1), 6)            AS peak_hour_ape,
        ROUND(AVG(f.forecast_error_mw) FILTER (WHERE f.is_daily_peak_hour = 1), 1) AS peak_hour_error_mw,
        ANY_VALUE(f.local_hour) FILTER (WHERE f.is_daily_peak_hour = 1)         AS peak_hour_local,

        -- Which named window absorbed the most absolute error today. This is
        -- the "where" in the generated narrative.
        ARG_MAX(h.day_period, f.abs_error_mw)                     AS worst_period,
        ARG_MAX(f.local_hour, f.abs_error_mw)                     AS worst_hour_local,
        ROUND(MAX(f.abs_error_mw), 1)                             AS worst_hour_error_mw,
        ROUND(MAX(f.ape_zscore), 2)                               AS max_ape_zscore,

        -- ---------- weather ----------
        ROUND(AVG(f.temp_f), 1)                                   AS avg_temp_f,
        ROUND(MAX(f.temp_f), 1)                                   AS max_temp_f,
        ROUND(MIN(f.temp_f), 1)                                   AS min_temp_f,
        ROUND(SUM(f.cooling_degree_hours), 1)                     AS cooling_degree_hours,
        ROUND(SUM(f.heating_degree_hours), 1)                     AS heating_degree_hours,

        -- ---------- provenance ----------
        MAX(f.is_demo_data)                                       AS is_demo_data,
        ROUND(AVG(f.weather_coverage), 3)                         AS avg_weather_coverage
    FROM fact_load_hour f
    LEFT JOIN dim_hour h ON h.hour_of_day = f.local_hour
    GROUP BY f.ba_code, f.date_key
),
baselined AS (
    SELECT
        d.*,
        -- Trailing 28 days of the SAME day type, current day excluded.
        AVG(d.mape) OVER (
            PARTITION BY d.ba_code, d.day_type
            ORDER BY d.date_key
            ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
        )                                                         AS mape_baseline_same_daytype,
        AVG(d.peak_demand_mw) OVER (
            PARTITION BY d.ba_code, d.day_type
            ORDER BY d.date_key
            ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
        )                                                         AS peak_baseline_same_daytype,
        -- HOW MANY days actually went into that average.
        -- This guard was added after testing exposed a real bug: on the fifth
        -- day of the dataset, a Saturday was compared against a "weekend
        -- baseline" built from a single prior Sunday, and the brief confidently
        -- announced peak load was "98% above baseline". The average existed, so
        -- nothing was NULL and nothing errored -- it was simply meaningless.
        --
        -- The lesson generalises: an average over a window is only as
        -- trustworthy as the number of observations in it, and a window function
        -- will never tell you that number unless you ask. Always count the
        -- baseline alongside the baseline.
        COUNT(d.mape) OVER (
            PARTITION BY d.ba_code, d.day_type
            ORDER BY d.date_key
            ROWS BETWEEN 28 PRECEDING AND 1 PRECEDING
        )                                                         AS baseline_sample_days,
        -- Simple previous-calendar-day values, for the literal "since yesterday"
        -- question. Kept alongside the like-for-like baseline so the dashboard
        -- can show both and the user can see when they disagree.
        LAG(d.mape)            OVER (PARTITION BY d.ba_code ORDER BY d.date_key) AS mape_prev_day,
        LAG(d.peak_demand_mw)  OVER (PARTITION BY d.ba_code ORDER BY d.date_key) AS peak_prev_day,
        LAG(d.mape, 7)         OVER (PARTITION BY d.ba_code ORDER BY d.date_key) AS mape_same_day_last_week
    FROM daily d
),
-- A separate CTE for the comparison columns, because the narrative below needs
-- to REFERENCE them. A SELECT list cannot reliably reuse its own aliases in
-- portable SQL, so we materialise them one step earlier. Splitting a query into
-- named stages like this is also simply easier to debug: each CTE can be
-- SELECTed on its own to see what it produced.
compared AS (
    SELECT
        b.* EXCLUDE (mape_baseline_same_daytype, peak_baseline_same_daytype),
        -- Suppress the baseline entirely below 4 same-day-type observations.
        -- Returning NULL is the honest answer: "we do not know yet" is a valid
        -- result, and it is far better than a confident wrong number. Every
        -- downstream comparison and the narrative all key off these, so one
        -- guard here protects the whole file.
        CASE WHEN b.baseline_sample_days >= 4 THEN b.mape_baseline_same_daytype END AS mape_baseline_same_daytype,
        CASE WHEN b.baseline_sample_days >= 4 THEN b.peak_baseline_same_daytype END AS peak_baseline_same_daytype,
        CASE WHEN b.baseline_sample_days >= 4
             THEN ROUND(b.mape - b.mape_baseline_same_daytype, 6) END               AS mape_vs_baseline,
        CASE WHEN b.baseline_sample_days >= 4
             THEN ROUND(b.mape / NULLIF(b.mape_baseline_same_daytype, 0) - 1, 4) END AS mape_vs_baseline_pct,
        -- PEAK comparisons carry an EXTRA condition: the day must be complete.
        --
        -- Found during testing: the current partial day reported "peak load 39%
        -- below baseline" at 08:00 local. Of course it was -- the day's peak had
        -- not happened yet. Comparing an in-progress day's running maximum
        -- against a full-day baseline is not a finding, it is a clock.
        --
        -- MAPE does NOT need this guard, because it is a mean over whatever
        -- hours exist and stays meaningful on a partial day. A MAX does not.
        -- Knowing WHICH aggregates survive incomplete data and which do not is
        -- the general lesson: means degrade gracefully, extremes do not.
        --
        -- 20 rather than 24 tolerates DST days (23h) and minor publication gaps.
        CASE WHEN b.baseline_sample_days >= 4 AND b.hours_with_actual >= 20
             THEN ROUND(b.peak_demand_mw - b.peak_baseline_same_daytype, 1) END      AS peak_vs_baseline_mw,
        CASE WHEN b.baseline_sample_days >= 4 AND b.hours_with_actual >= 20
             THEN ROUND(b.peak_demand_mw / NULLIF(b.peak_baseline_same_daytype, 0) - 1, 4) END AS peak_vs_baseline_pct,
        CASE WHEN b.hours_with_actual >= 20 THEN 1 ELSE 0 END                        AS is_complete_day
    FROM baselined b
)
SELECT
    b.*,

    -- ---------- status band ----------
    -- Three-state traffic light, thresholded on the RATIO to the like-for-like
    -- baseline rather than an absolute MAPE. That matters because 3% MAPE is
    -- excellent for ISO-NE and mediocre for PJM -- a relative threshold treats
    -- each BA against its own standard instead of a one-size-fits-all bar.
    CASE
        WHEN b.mape_baseline_same_daytype IS NULL              THEN 'Insufficient history'
        WHEN b.mape > b.mape_baseline_same_daytype * 1.75      THEN 'Attention'
        WHEN b.mape > b.mape_baseline_same_daytype * 1.25      THEN 'Watch'
        ELSE 'Normal'
    END                                                                      AS status_band,

    -- ---------- generated narrative ----------
    -- Built with concatenation, not a template engine, so it stays inspectable.
    -- Structure follows the good-insight pattern: MAGNITUDE + COMPARISON +
    -- LOCATION + LIKELY DRIVER. Never just a number.
    CASE
        WHEN b.hours_with_actual = 0 THEN
            'No actual demand published for this date yet.'
        WHEN b.mape_baseline_same_daytype IS NULL THEN
            'Day-ahead MAPE ' || printf('%.2f', b.mape * 100) || '%. '
            || 'Insufficient history for a like-for-like comparison.'
        ELSE
            'Day-ahead MAPE ' || printf('%.2f', b.mape * 100) || '% versus a '
            || printf('%.2f', b.mape_baseline_same_daytype * 100) || '% '
            || lower(b.day_type) || ' baseline ('
            || CASE WHEN b.mape >= b.mape_baseline_same_daytype THEN '+' ELSE '' END
            || printf('%.0f', b.mape_vs_baseline_pct * 100)
            || '%). Error concentrated in the ' || lower(COALESCE(b.worst_period, 'unknown window'))
            || ', peaking at ' || printf('%02d:00', COALESCE(b.worst_hour_local, 0))
            || ' local ('  || printf('%.0f', COALESCE(b.worst_hour_error_mw, 0)) || ' MW). '
            -- Wording adapts to completeness: an in-progress day reports a
            -- "highest so far", never a peak, and never a baseline comparison.
            -- Saying "so far" costs three words and prevents a reader drawing a
            -- conclusion the data cannot support.
            || CASE WHEN b.is_complete_day = 1 THEN 'Peak load ' ELSE 'Highest load so far ' END
            || printf('%.0f', b.peak_demand_mw) || ' MW at '
            || printf('%02d:00', COALESCE(b.peak_hour_local, 0))
            || CASE
                 WHEN b.is_complete_day = 0
                   THEN ' (day in progress, ' || b.hours_with_actual || ' of 24 hours published)'
                 WHEN b.peak_vs_baseline_pct IS NULL THEN ' (no baseline available)'
                 WHEN b.peak_vs_baseline_pct >= 0
                   THEN ', ' || printf('%.0f', b.peak_vs_baseline_pct * 100) || '% above baseline'
                 ELSE ', ' || printf('%.0f', ABS(b.peak_vs_baseline_pct) * 100) || '% below baseline'
               END
            || '. '
            -- Attribute a likely driver. Deliberately hedged wording
            -- ("consistent with", "coincided with") because correlation at
            -- daily grain is suggestive, not proof. Overclaiming causation is
            -- the fastest way to lose a technical interviewer's trust.
            || CASE
                 WHEN b.cooling_degree_hours > 120 AND b.avg_error_mw > 0
                   THEN 'Under-forecasting on a high cooling-load day, consistent with '
                        || 'an under-modelled air-conditioning response.'
                 WHEN b.cooling_degree_hours > 120
                   THEN 'High cooling load (' || printf('%.0f', b.cooling_degree_hours)
                        || ' CDH) was the dominant demand driver.'
                 WHEN b.heating_degree_hours > 250
                   THEN 'High heating load (' || printf('%.0f', b.heating_degree_hours)
                        || ' HDH) was the dominant demand driver.'
                 WHEN b.day_type = 'Holiday'
                   THEN 'Holiday load shape (' || COALESCE(b.holiday_name, 'holiday')
                        || '), where forecasts have few historical analogues.'
                 WHEN b.max_ape_zscore > 3
                   THEN 'Contains at least one hour more than 3 standard deviations '
                        || 'from its own 30-day error distribution.'
                 ELSE 'No single dominant weather driver identified.'
               END
    END                                                                      AS daily_narrative
FROM compared b
ORDER BY b.ba_code, b.date_key;
