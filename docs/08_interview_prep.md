# Interview Preparation

70 questions, grouped. Each lists **what a strong answer contains** — not a script to memorise, but the points that must appear.

> **The one rule:** every answer should reference something specific from *this* project. "I'd use a star schema" is a textbook answer. "I used a star schema with two facts at different grains sharing conformed dimensions, because merging fuel mix into the load fact would have created a fan trap that multiplied demand by eight" is your answer.

---

## A. Technical / Data Engineering (20)

**1. Walk me through your pipeline end to end.**
Source → orchestration → raw → transform → serve. Name the technology and the *reason* at each hop. Land the punchline: raw is immutable, so a transform bug is a replay, not a re-download.

**2. Why DuckDB rather than Postgres?**
The real reason: Power BI Service can't schedule refresh against cloud Postgres without an on-premises gateway on an always-on machine. Not $0, not demoable from a laptop. DuckDB is in-process. Add that the SQL ports nearly unchanged.

**3. What happens if the EIA API changes?**
The metadata probe runs and commits the API's self-description, so drift appears as a git diff. Plus explicit typing in Power Query fails loudly, and 12 DQ checks gate the build. Distinguish *detected* from *prevented* — you detect.

**4. Your job runs 24×/day. How do you handle a transient failure?**
Exponential backoff with jitter; retry 429/5xx, never 4xx. Explain jitter: without it, parallel failures retry in lockstep. Explain never-retrying-4xx: it wastes quota and hides a real bug.

**5. What does idempotent mean and why does yours need to be?**
Running twice = running once. Needed because the hourly job re-pulls 10 days (EIA revises), and a blind append would duplicate hours and double-count demand. Upsert on the natural key with `keep=last`.

**6. Why re-pull 10 days instead of just the last hour?**
EIA back-fills and revises; a T+1h value is provisional. The window trades a little bandwidth for correctness. Also self-heals a missed run.

**7. Why Parquet for raw and CSV alongside curated?**
Parquet: ~15× smaller, carries types, columnar. CSV: portability, Excel-openable, human-inspectable on GitHub. Different consumers, different constraints.

**8. How do you know your data is correct?**
Twelve materialised assertions: grain uniqueness, referential integrity both directions, calendar contiguity, arithmetic consistency, domain plausibility (MAPE in 0.2–15%), freshness. `FAIL` severity stops the build. They're on a dashboard page.

**9. Which check would catch the worst bug?**
`fact_grain_unique`. Duplicate rows inflate every aggregate while still looking plausible — the most dangerous silent failure in dimensional modelling.

**10. Describe the hardest bug you hit.**
The baseline-sample bug. A window average over one observation produced *"98% above baseline"* — no error, no null, completely meaningless. Fix: `COUNT() OVER` the same frame, suppress below 4. The lesson generalises: an average is only as trustworthy as its n, and a window function never volunteers that count.

**11. How do you handle timezones?**
Raw is UTC, session pinned to UTC, one explicit `AT TIME ZONE` conversion via ICU. Local date/hour derived there and only there. Why it matters: fixed offsets are wrong for half the year, and an hour-of-day analysis shifted 4–8 hours is silently wrong.

**12. Why is local hour rather than UTC hour the right grain for the analysis?**
Load follows human behaviour — people wake, work and cool their homes on local time. Six BAs across four time zones compared on UTC hour would smear every peak.

**13. Two weather endpoints. Why, and what's the risk?**
Archive (ERA5) lags ~5 days; forecast covers recent past + future. Neither alone spans the range. Risk is a silent 5-day hole in the newest data. Mitigated with deliberate overlap and deterministic de-dup preferring archive.

**14. Your weather is a proxy. Defend it.**
Population-weighted blend of 4–6 metros per BA, weights declared in config so they're criticisable. Acknowledge it understates sprawling footprints (MISO). Note the renormalise-on-present-weight detail — a fixed denominator would invent cold snaps from missing data.

**15. Why 25 months of history?**
YoY needs a full prior year *plus* the current partial year. At 12 months `SAMEPERIODLASTYEAR` returns blank everywhere and half the measure library is dead.

**16. How would this scale to 60 BAs and 5-minute data?**
Volume goes ~120×. Parquet in Git stops being appropriate — move to object storage (R2/S3, still near-free), keep DuckDB or move to a warehouse, switch Power BI to incremental refresh. Note the honest breakpoint: Git is fine at ~110k rows/refresh, not at 13M.

**17. What breaks first at scale?**
The Git-as-storage layer, then Power BI Import mode's 1 GB free limit. Not the SQL.

**18. How do you monitor it?**
GitHub emails on scheduled-workflow failure; run ledger CSV; freshness stamp surfaced as a KPI on both the site and the report. A dashboard that hides its own staleness is worse than none.

**19. Why `:12` past the hour?**
GitHub cron is best-effort and hugely oversubscribed at `:00`. Off-peak minutes are measurably more reliable and cost nothing.

**20. What would you do differently?**
Genuine answers: add unit tests for the transform (currently only integration-level DQ checks); add a proper backfill checkpoint so a mid-backfill failure resumes; consider dbt if the SQL grew past ~20 models.

---

## B. Power BI (20)

**1. Star schema vs one flat table — why?**
Slicer cardinality, compression on repeated text, and — the decisive one — two facts at different grains need shared conformed dimensions so one BA slicer filters both consistently.

**2. What is a conformed dimension?**
A dimension shared by multiple facts so filters apply consistently across them. Here `dim_ba`, `dim_date`, `dim_hour` are conformed across `fact_load_hour` and `fact_fuel_mix`.

**3. Why not merge fuel mix into the load fact?**
Different grain. Merging forces either an 8-column pivot (breaks on a new fuel type, unsliceable) or an 8× fan-out that multiplies demand by eight. That's a fan trap, dangerous because totals look plausible.

**4. Single vs bidirectional filtering?**
Single, always, by default. Bidirectional creates ambiguous filter paths with two facts on a shared dimension. If one measure needs reverse filtering, use `CROSSFILTER()` locally rather than changing the model globally.

**5. Why mark a date table?**
Time intelligence walks the calendar row by row. Unmarked or gapped, `SAMEPERIODLASTYEAR` returns **wrong values without erroring** — which is why the DQ suite includes a contiguity check.

**6. Import vs DirectQuery here?**
Import. The data is refreshed on our schedule anyway, the model is ~110k rows, and Import gets VertiPaq compression and fast DAX. DirectQuery would add latency for no freshness gain, since the source is a static file.

**7. Explain your MAPE measure.**
`AVERAGE(ape)` filtered to `row_kind = "actual"`. Then the two things that matter: (a) it's the mean of ratios, not the ratio of sums — industry convention, weights every hour equally; (b) the `row_kind` filter is mandatory or future rows dilute it toward zero silently.

**8. Why does bias matter more than MAPE?**
MAPE is noise, largely irreducible. Bias is systematic direction, fixable by recalibration. A 3% MAPE / 0% bias forecast is doing its job; 3% / +2% is losing money the same way every day.

**9. Walk me through the regression slope in DAX.**
Least squares: `(n·Σxy − Σx·Σy) / (n·Σxx − (Σx)²)` via `SUMX` over a filtered table. Business meaning: MW per cooling degree hour — translates a weather forecast into a capacity requirement. Mention the `N > 30` guard: a slope from eight points is noise wearing a number's clothing.

**10. Calculated column vs measure?**
Column = row-level, materialised, filter-independent. Measure = aggregate, evaluated in filter context. Rule used here: row-level deterministic → SQL (not even a calculated column); aggregate/filter-dependent → measure. A pre-computed MAPE column would be wrong the instant someone filtered to weekdays.

**11. `CALCULATE` — what does it actually do?**
Modifies filter context. Its arguments replace filters on the columns they touch (not intersect). Reference `CALCULATE([MAPE], row_kind="actual")`.

**12. `DIVIDE` vs `/`?**
`DIVIDE` returns BLANK on divide-by-zero; BLANK renders as an empty cell rather than breaking the visual.

**13. Why does the rolling baseline exclude the current day?**
Data leakage. If today were in its own baseline, an extreme day would inflate the benchmark it's judged against and partly conceal itself. Same principle as the SQL window frame.

**14. Where did you use a field parameter and why?**
Page 2 — one chart answers four metric questions instead of four charts crowding the canvas. Note the `Selected Metric Formatted` measure, because percentages and MW need different format strings.

**15. Your what-if parameter produces dollar figures. Defend that.**
It's an explicit user-set **scenario**, never a measured result. Real imbalance settlement prices aren't free data. The visual is labelled "illustrative scenario — assumes $X/MWh", and the resume bullets never quote a dollar impact. Volunteering this before being asked is the point.

**16. Why RLS *not* used?**
No multi-tenant audience on a public dataset. Adding it would be theatre. Say where it *would* apply — the hospital-site variant of the pharmacy project.

**17. A refresh that works in Desktop fails in the Service. Why?**
Dynamic data source. String-concatenated URLs can't be permission-verified before execution. Fix: `Web.Contents(Base, [RelativePath = "..."])`.

**18. Why RelativePath and a no-cache header?**
RelativePath for refresh eligibility; `Cache-Control: no-cache` so an hourly refresh sees the new commit rather than a 40-minute-old cached copy.

**19. Why a function in Power Query instead of eight queries?**
DRY. The URL pattern, RelativePath fix, format switch and error handling exist once. Changing any of them is a one-line edit rather than eight, one of which you'd miss.

**20. Which Power BI features did you deliberately *not* use?**
RLS, the forecasting visual (this report *grades* forecasts — layering a naive one on top is confusing), maps (BA footprints are irregular polygons Power BI can't render from a code; a wrong map is worse than none), AI narrative (we generate a better deterministic one in SQL). Being able to justify omissions is worth as much as features.

---

## C. SQL (10)

**1. Why `MAX(CASE WHEN ...)` instead of `PIVOT`?**
Portability (runs on Postgres/Snowflake/BigQuery unchanged) and graceful duplicate handling — `MAX` collapses a duplicate key deterministically rather than fanning out. Mention that aggregates ignore NULLs, which is the whole trick.

**2. Explain your window frame.**
`ROWS BETWEEN 720 PRECEDING AND 1 PRECEDING` — 30 days, current row excluded, to avoid leakage. Contrast `ROWS` (physical rows) with `RANGE` (logical value range).

**3. Why `FULL OUTER JOIN`?**
Three horizons of different lengths. `INNER` keeps only the overlap and discards the forward weather the peak-risk page needs. `row_kind` labels each row's nature so error measures can exclude non-gradeable rows.

**4. INNER vs LEFT — where did you use each and why?**
`INNER` from fact to `dim_ba`: a row for an unknown BA is meaningless and must not enter the model — referential integrity enforced at build time rather than discovered as a blank category later. `LEFT` to `seed_holiday`: most days aren't holidays.

**5. How do you dedupe deterministically?**
`ROW_NUMBER() OVER (PARTITION BY key ORDER BY tiebreak)` then `WHERE rn = 1`. Here the tiebreak is source name, exploiting `'archive' < 'forecast'` so the better product wins with no special-casing.

**6. Write a query for each BA's worst forecast day last month.**
Expect `ROW_NUMBER()`/`QUALIFY` or a correlated subquery. Strong answers mention ties and whether `RANK` or `ROW_NUMBER` is wanted.

**7. `HAVING COUNT(*) >= 20` in the temperature profile — why?**
Suppresses buckets too thin to be meaningful. Same principle as the baseline-sample bug: don't publish a statistic computed from almost nothing.

**8. Why is `NULLIF` everywhere?**
Guards division by zero, returning NULL instead of an error. A zero or negative demand reading is itself bad data, so NULL is the honest output.

**9. How would you test SQL transformations?**
Currently: 12 materialised assertions gating the build. Better: seed a tiny fixture with known-answer expectations and assert exact outputs — proper unit tests. Name this as a known gap; admitting it reads better than pretending.

**10. This runs on DuckDB. What changes on Postgres?**
Very little — CTEs, window functions, `FULL OUTER JOIN`, `FILTER` are all standard. `AT TIME ZONE` works. Differences: `read_parquet` → a foreign table or COPY, `ARG_MAX` → `DISTINCT ON` or a window, `ANY_VALUE` → `MIN`.

---

## D. Business Analysis (10)

**1. Who uses this and what decision does it change?**
Load forecasting analyst / procurement manager. Decision: whether to open a model recalibration ticket, and where to focus it. The bias-vs-CDH finding says *when* the model is wrong, which is what makes it actionable rather than merely interesting.

**2. Turn your main finding into one sentence for an executive.**
"Our day-ahead forecast is unbiased in mild weather but under-predicts load by roughly 3% once cooling demand passes 15 degree-hours — meaning we're systematically short on exactly the days power is most expensive."

**3. Why is bias more actionable than error size?**
Noise is irreducible; direction is a modelling gap. You can't fix randomness, you can recalibrate a systematic response curve.

**4. Someone says "3% error is fine." Respond.**
Two moves: (a) at PJM scale 1% ≈ 1,000 MW, so 3% is ~3,000 MW; (b) the average conceals the distribution — peak-hour and extreme-heat error are materially worse, and those are the hours that settle at scarcity prices.

**5. What would you need to quantify the dollar impact properly?**
Real-time vs day-ahead settlement price spreads by interval and market. Not free data. Say plainly you didn't have it and therefore never claimed a dollar impact.

**6. How would you validate the finding before acting?**
Check it holds out-of-sample on a later period; check it's not an artefact of the weather proxy by testing alternative city weightings; check whether the operator's published methodology already accounts for AC response.

**7. What's the counter-argument to your conclusion?**
The weather proxy is coarse, so some apparent bias may be proxy error rather than model error. Also, operators may under-forecast *deliberately* in some markets for reserve-procurement reasons. Volunteering this is a strength.

**8. What KPI would you put on an executive's one-pager?**
Peak-hour bias, by BA, trended monthly. It's the intersection of "systematic and fixable" with "the hours that cost most."

**9. How do you decide what NOT to put on a dashboard?**
Every visual must answer a stated business question. If it can't, it's decoration competing for attention with the things that matter.

**10. What's the next analysis you'd run?**
Net load (demand minus wind and solar) rather than gross demand — as variable generation share rises, you're forecasting two weather processes and subtracting them, which is where error is growing fastest industry-wide.

---

## E. Hiring Manager (10)

1. **Why this project?** — Started from the business problem (forecast error costs money), then found data that could grade a decision, not just describe an outcome.
2. **How long did it take?** — Be honest and specific. Break it down: probing APIs, pipeline, SQL, model, report, site.
3. **What was hardest?** — The baseline-sample bug. Silent and plausible, unlike the loud ones.
4. **What are you proudest of?** — The DQ gate and the fact the dashboard shows its own data quality.
5. **What's weakest?** — The weather proxy, and the absence of true unit tests on the transform.
6. **Did you use AI?** — Answer honestly and specifically about what you directed vs. what was generated, and demonstrate you can defend every design decision. The defensibility is what's being tested.
7. **What would you add with another week?** — Net-load analysis; unit tests; a second forecast source to compare against.
8. **How would you hand this to a teammate?** — Point to the runbook, the build log, and config-as-single-source-of-truth.
9. **What did you learn?** — Something specific and technical, e.g. that the dangerous failures are the silent plausible ones, not the loud ones.
10. **Why should we hire you?** — Tie to the role: end-to-end ownership from business question through pipeline to a decision-support product, with honest treatment of limitations.

---

## F. Questions that expose whether you actually built it

**Be able to answer all of these without hesitating.** These are the ones that separate builders from people who cloned a repo.

| Question | What only a builder knows |
|---|---|
| What's in `row_kind` and why does it exist? | Three horizons, `FULL OUTER JOIN`, the MAPE-dilution trap |
| What does `is_daily_peak_hour` partition by? | `(ba_code, date_key)` — local date, not UTC |
| Why 720 and why `1 PRECEDING`? | 30 days; excluding the current row prevents leakage |
| What breaks if you drop `AT TIME ZONE`? | Hour-of-day analysis shifts 4–8h; DST makes it seasonal |
| Sign convention on `forecast_error_mw`? | `actual − forecast`; positive = under-forecast = short |
| Why is `ape` stored as 0.0234 not 2.34? | Store the number, format at the edge |
| What is `baseline_sample_days` for? | The bug it fixed — verbatim from the build log |
| Why does `is_complete_day` gate peak but not MAPE? | Means degrade gracefully over partial data; extremes don't |
| Which DQ check is `FAIL` vs `WARN`, and why? | `ba_day_hour_coverage` is WARN because DST days legitimately have 23/25 hours |
| Why `:12` past the hour? | GitHub cron oversubscription at `:00` |
| What's `UseParquet` for? | Format switch; CSV fallback for older Desktop builds |
| Why does `fnGetTable` have `Enable load` off? | It's a function, not a table |
| What happens if you concatenate the URL? | Dynamic data source; cloud refresh fails, Desktop works |
| Why does the site need an HTTP server? | Browsers block `fetch()` on `file://` |
| Why were charts 0×0 on hidden tabs? | Chart.js can't measure a `display:none` canvas |
| Why string-slice dates instead of `new Date()`? | UTC-midnight parsing shows the previous day west of Greenwich |
| What's the MISO weather proxy weakness? | North–south sprawl; Minneapolis and New Orleans in one blend |
| What does `weather_coverage < 1` mean? | Some cities didn't report; denominator renormalised on present weight |
| Why is `dq_checks` unrelated to the facts? | It describes the pipeline, not the data — no meaningful key |
