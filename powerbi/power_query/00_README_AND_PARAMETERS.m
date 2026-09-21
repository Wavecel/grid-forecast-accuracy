// =============================================================================
// POWER QUERY SETUP -- read this before creating any query
// =============================================================================
//
// STEP 1 -- CREATE TWO PARAMETERS
// Home > Transform data > Manage Parameters > New Parameter
//
//   Name:  GitHubBaseUrl
//   Type:  Text
//   Value: https://raw.githubusercontent.com/<YOUR-USER>/grid-forecast-accuracy/main
//
//   Name:  UseParquet
//   Type:  Logical (True/False)
//   Value: true
//
// Parameterising the base URL means forking the repo or renaming a branch is a
// one-field change, not a 9-query find-and-replace.
//
//
// =============================================================================
// STEP 2 -- THE MOST IMPORTANT THING IN THIS ENTIRE FILE
// =============================================================================
//
// THE DYNAMIC DATA SOURCE TRAP
// ----------------------------
// This works perfectly in Power BI Desktop and then FAILS on every scheduled
// refresh in the Power BI Service:
//
//     Web.Contents( GitHubBaseUrl & "/data/curated/fact_load_hour.parquet" )   // WRONG
//
// Error you will get:  "You can't schedule refresh for this dataset because
// one or more sources currently don't support refresh."
//
// WHY: the Service must verify permissions on a data source BEFORE running the
// query. When the URL is assembled by string concatenation, the Service cannot
// determine the final URL without executing the query -- a chicken-and-egg it
// refuses to resolve. It calls this a "dynamic data source".
//
// THE FIX: pass the fixed base to Web.Contents and the variable part in the
// RelativePath option. Now the Service sees a static, verifiable base URL:
//
//     Web.Contents( GitHubBaseUrl, [ RelativePath = "data/curated/fact.parquet" ] )   // RIGHT
//
// EVERY query below uses RelativePath. Do not "simplify" them back to
// concatenation -- it will work on your laptop and break in the cloud, which is
// the worst possible failure mode because you will not notice until the data
// goes stale.
//
// This is also an excellent interview answer to "what problems have you hit
// publishing Power BI reports?".
//
//
// =============================================================================
// STEP 3 -- WHY PARQUET RATHER THAN CSV
// =============================================================================
//   * ~10x smaller  -> faster refresh, less bandwidth
//   * Carries real data types -> no "is this column a date or text?" guessing,
//     and no locale bugs when the Service (running in a different region than
//     your laptop) parses "03/04/2026" as 4 March or 3 April
//   * Columnar -> Power Query only reads the columns you keep
//
// CSV equivalents are published alongside as a fallback and for human
// inspection. The UseParquet parameter switches between them.
//
//
// =============================================================================
// STEP 4 -- QUERY LOAD SETTINGS  (easy to get wrong, costly when you do)
// =============================================================================
//   fnGetTable ................. Enable load = OFF   (helper function)
//   dim_ba, dim_date, dim_hour,
//   dim_fuel, fact_load_hour,
//   fact_fuel_mix, dq_checks ... Enable load = ON
//   agg_daily_brief ............ Enable load = ON
//
// Right-click a query > uncheck "Enable load" for helpers. A helper function
// loaded as a table clutters the model and confuses anyone reading it later.
//
//
// =============================================================================
// STEP 5 -- AFTER LOADING, IN THE MODEL VIEW
// =============================================================================
//   1. Mark dim_date as a date table:
//        Table tools > Mark as date table > date_key
//      Time-intelligence DAX returns WRONG (not error) results without this.
//   2. Build relationships -- see docs/04_data_model.md
//   3. Hide every foreign key column on the fact tables from Report view.
//      A user who drags fact_load_hour[date_key] onto an axis instead of
//      dim_date[date_key] gets a chart that ignores time intelligence and
//      looks fine. Hiding the key removes the trap.
// =============================================================================
