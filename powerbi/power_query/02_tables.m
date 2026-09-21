// =============================================================================
// TABLE QUERIES -- one per curated table
// =============================================================================
// Create each as a Blank Query, paste the block, rename to match the comment.
// All of them are short, because fnGetTable already did the hard part. That is
// the payoff of factoring the ingestion logic out once.
//
// A NOTE ON EXPLICIT TYPING
// -------------------------
// Even when reading Parquet (which carries types), every query below re-asserts
// them with Table.TransformColumnTypes. Two reasons:
//   1. It is a CONTRACT. If an upstream change alters a column's type, the query
//      fails here with a clear message rather than silently loading numbers as
//      text -- at which point your measures return blank and you spend an hour
//      hunting a phantom.
//   2. It documents the expected schema right where a reader is looking.
// The cost is a few lines. The benefit is that schema drift becomes loud.
// =============================================================================


// ============================ QUERY: dim_ba ==================================
let
    Source = fnGetTable("dim_ba"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "ba_code", type text }, { "ba_name", type text },
                { "market", type text }, { "timezone", type text },
                { "peak_season", type text }, { "weather_city_count", Int64.Type },
                { "weather_cities", type text }, { "ba_label", type text },
                { "size_band", type text }, { "region", type text }
            }
        )
in
    Typed


// =========================== QUERY: dim_date =================================
let
    Source = fnGetTable("dim_date"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "date_key", type date },
                { "calendar_year", Int64.Type }, { "calendar_quarter", Int64.Type },
                { "quarter_label", type text }, { "month_number", Int64.Type },
                { "month_name", type text }, { "month_short", type text },
                { "year_month", Int64.Type }, { "year_month_label", type text },
                { "day_of_month", Int64.Type }, { "day_of_year", Int64.Type },
                { "iso_week", Int64.Type }, { "iso_day_of_week", Int64.Type },
                { "day_name", type text }, { "day_short", type text },
                { "is_weekend", Int64.Type }, { "is_holiday", Int64.Type },
                { "holiday_name", type text }, { "day_type", type text },
                { "season", type text }, { "is_summer_peak_season", Int64.Type },
                { "days_ago", Int64.Type }, { "is_today", Int64.Type },
                { "is_yesterday", Int64.Type }, { "is_future", Int64.Type },
                { "is_last_7_days", Int64.Type }, { "is_last_30_days", Int64.Type }
            }
        )
in
    Typed
// AFTER LOADING: Table tools > Mark as date table > date_key
// Skip this and SAMEPERIODLASTYEAR returns wrong numbers WITHOUT erroring.


// =========================== QUERY: dim_hour =================================
let
    Source = fnGetTable("dim_hour"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "hour_of_day", Int64.Type }, { "hour_label", type text },
                { "hour_range_label", type text }, { "day_period", type text },
                { "day_period_sort", Int64.Type },
                { "is_system_peak_window", Int64.Type },
                { "is_solar_ramp_window", Int64.Type },
                { "is_overnight_trough_window", Int64.Type }
            }
        )
in
    Typed
// AFTER LOADING: select day_period > Column tools > Sort by column >
// day_period_sort. Without it, 'Evening peak' sorts before 'Morning ramp'
// alphabetically and the chart tells a story that never happened.


// =========================== QUERY: dim_fuel =================================
let
    Source = fnGetTable("dim_fuel"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "fuel_code", type text }, { "fuel_name", type text },
                { "fuel_category", type text }, { "is_renewable", Int64.Type },
                { "is_variable", Int64.Type }, { "fuel_sort", Int64.Type }
            }
        )
in
    Typed


// ======================== QUERY: fact_load_hour ==============================
// The big one: roughly 110,000 rows at full 25-month history.
let
    Source = fnGetTable("fact_load_hour"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "ba_code", type text },
                { "period_utc", type datetimezone },
                { "local_datetime", type datetime },
                { "date_key", type date },
                { "local_hour", Int64.Type },
                { "row_kind", type text }, { "day_type", type text },
                { "holiday_name", type text }, { "is_demo_data", Int64.Type },
                { "demand_mw", type number }, { "forecast_mw", type number },
                { "net_generation_mw", type number }, { "interchange_mw", type number },
                { "ramp_mw", type number },
                { "forecast_error_mw", type number }, { "abs_error_mw", type number },
                { "ape", type number }, { "signed_pe", type number },
                { "temp_f", type number }, { "humidity_pct", type number },
                { "wind_mph", type number }, { "cloud_pct", type number },
                { "cooling_degree_hours", type number },
                { "heating_degree_hours", type number },
                { "temp_change_1h", type number },
                { "is_daily_peak_hour", Int64.Type }, { "is_material_miss", Int64.Type },
                { "ape_zscore", type number }, { "ape_roll30_mean", type number },
                { "demand_roll30_mean", type number }, { "roll30_sample_hours", Int64.Type },
                { "weather_coverage", type number }, { "weather_source", type text },
                { "series_rows_present", Int64.Type },
                { "missing_weather_flag", Int64.Type }, { "missing_forecast_flag", Int64.Type }
            }
        ),

    // A load-time contract check. Table.RowCount forces evaluation, so a
    // catastrophically empty publish (a failed pipeline run that committed an
    // empty file) fails the REFRESH rather than silently emptying the report.
    // A blank dashboard looks like a Power BI problem and sends you hunting in
    // entirely the wrong place; a failed refresh sends you straight to the data.
    Guarded =
        if Table.RowCount( Typed ) = 0 then
            error Error.Record(
                "EmptyTable",
                "fact_load_hour loaded 0 rows",
                "The pipeline likely published an empty file. Check the latest GitHub Actions run."
            )
        else
            Typed
in
    Guarded


// ======================== QUERY: fact_fuel_mix ===============================
let
    Source = fnGetTable("fact_fuel_mix"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "ba_code", type text }, { "period_utc", type datetimezone },
                { "date_key", type date }, { "local_hour", Int64.Type },
                { "fuel_code", type text }, { "generation_mw", type number },
                { "total_generation_mw", type number }, { "generation_share", type number }
            }
        )
in
    Typed


// ======================= QUERY: agg_daily_brief ==============================
// Day grain. Carries the generated narrative text for the Daily Brief page.
let
    Source = fnGetTable("agg_daily_brief"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "ba_code", type text }, { "date_key", type date },
                { "day_type", type text }, { "holiday_name", type text },
                { "hours_with_actual", Int64.Type }, { "hours_total", Int64.Type },
                { "avg_demand_mw", type number }, { "peak_demand_mw", type number },
                { "min_demand_mw", type number }, { "total_demand_mwh", type number },
                { "peak_to_trough_mw", type number }, { "max_abs_ramp_mw", type number },
                { "mape", type number }, { "bias_pct", type number },
                { "avg_error_mw", type number }, { "total_abs_error_mwh", type number },
                { "worst_hour_ape", type number }, { "material_miss_hours", Int64.Type },
                { "peak_hour_ape", type number }, { "peak_hour_error_mw", type number },
                { "peak_hour_local", Int64.Type }, { "worst_period", type text },
                { "worst_hour_local", Int64.Type }, { "worst_hour_error_mw", type number },
                { "max_ape_zscore", type number },
                { "avg_temp_f", type number }, { "max_temp_f", type number },
                { "min_temp_f", type number },
                { "cooling_degree_hours", type number }, { "heating_degree_hours", type number },
                { "is_demo_data", Int64.Type }, { "avg_weather_coverage", type number },
                { "baseline_sample_days", Int64.Type },
                { "mape_baseline_same_daytype", type number },
                { "peak_baseline_same_daytype", type number },
                { "mape_prev_day", type number }, { "peak_prev_day", type number },
                { "mape_same_day_last_week", type number },
                { "mape_vs_baseline", type number }, { "mape_vs_baseline_pct", type number },
                { "peak_vs_baseline_mw", type number }, { "peak_vs_baseline_pct", type number },
                { "status_band", type text }, { "daily_narrative", type text }
            }
        )
in
    Typed


// =========================== QUERY: dq_checks ================================
// Twelve rows. Powers the "Data & Pipeline" page.
//
// Loading your own quality checks into the report is unusual and worth doing:
// it lets the dashboard answer "how do you know this data is right?" on screen,
// instead of that question hanging over the whole thing.
let
    Source = fnGetTable("dq_checks"),
    Typed =
        Table.TransformColumnTypes(
            Source,
            {
                { "check_name", type text }, { "severity", type text },
                { "result", type text }, { "observed", Int64.Type },
                { "expected", Int64.Type }, { "description", type text }
            }
        )
in
    Typed
