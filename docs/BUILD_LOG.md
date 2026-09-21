# Build Log

A record of every step taken, in order, including the things that went wrong.

**Why keep this.** Three reasons, and the third is the real one:
1. It makes the project reproducible.
2. It documents *why* decisions were made, not just what they were.
3. **The bugs are the most interview-valuable content in the repo.** "Tell me about a bug you found" is a guaranteed question, and a specific, technical, honest answer with a real fix beats a rehearsed generality every time. Four real ones are recorded below.

---

## Step 0 — Environment check

```
Python 3.13.3 · git 2.49.0 · pandas 2.3.2 · requests 2.32.5
Missing: duckdb, pyarrow  →  pip install duckdb pyarrow
```

**Why check first:** knowing DuckDB was absent shaped the architecture decision that followed. Discovering a missing dependency after writing 500 lines against it is avoidable waste.

---

## Step 1 — Verify the APIs actually work *before* writing a pipeline

**Why this order.** The most expensive mistake in a data project is building the whole stack on an assumption about a data source, then discovering the source doesn't behave the way you assumed. Ten minutes of probing first.

**Open-Meteo — verified live, fully:**

| Test | Result |
|---|---|
| Forecast endpoint, no key | 120 hours returned |
| Archive endpoint, 2024 dates | 72 hours returned |
| Reaches into the future | Yes — +2 days |

**EIA — could not verify without a key.** Both a real route and a deliberately fake route returned `403`, so an unauthenticated probe cannot distinguish "route exists" from "route doesn't".

**Response to that limitation:** built `probe_route()` and `probe_available_respondents()` into the client, and made `python -m src.ingest --probe` the first command in the runbook. The API's self-description gets committed to git, so schema drift shows up as a **diff** rather than as a mysterious 3am failure. A constraint turned into a feature.

---

## Step 2 — Configuration as the single source of truth

`src/config.py` — 6 balancing authorities, 31 weather cities with population weights, degree-hour base, thresholds.

**Why one declarative file:** every assumption a reviewer might challenge lives in one auditable place. "How did you decide Boston represents ISO New England?" is answerable by pointing at one file, not by reading three.

**The BAs and why these six** — they span genuinely different load-shape regimes, so the analysis has variety to find rather than the same result six times:

| BA | Regime |
|---|---|
| ERCO | Summer-peaking, extreme cooling, scarcity events |
| CISO | Solar-saturated duck curve, steep evening ramps |
| ISNE | Dual peak, small and weather-whippy |
| NYIS | Dense urban, sharp summer peaks |
| PJM | Largest US market, mixed climate, stable |
| MISO | North–south sprawl — its weather proxy is *deliberately* hard |

---

## Step 3 — Resilient HTTP + a run ledger

`src/utils.py`

**Why retry logic is not optional:** a job running 24×/day will hit a transient 503 every few days. If one failed call kills the run, the dashboard silently goes stale and nobody notices for a week. That is how portfolio "live dashboards" die.

Design points:
- Exponential backoff **with jitter** — without jitter, parallel failures retry in lockstep and create a self-inflicted thundering herd
- Retries `429`/`5xx`; **never** retries `4xx` — retrying a bad API key wastes quota and hides a real bug
- Honours `Retry-After` when the server sends it
- Every run appends to `data/logs/run_log.csv`, so failure is *visible* and chartable

---

## Step 4 — Weather client, and the seam that breaks pipelines

`src/weather_client.py`

The problem: neither Open-Meteo endpoint alone covers "25 months ago → tomorrow".

```
[ ------------ archive (ERA5, lags ~5 days) ------------ ][ overlap ][ - forecast - ]
                                                           ↑ de-duplicate here
```

Get this wrong and you get a **silent 5-day hole in the most recent data** — exactly the window the daily brief depends on. It's invisible until someone asks why yesterday is blank.

**Solution:** request generous overlap deliberately, then resolve deterministically. `'archive' < 'forecast'` alphabetically, so an ascending sort + `keep first` prefers the higher-quality reanalysis product with no special-case logic.

**The weighting subtlety worth being able to explain:** the weighted mean divides by the weight of cities that *actually reported*, not by full declared weight. If Fresno has a gap, a fixed denominator would drag California's blended temperature toward zero and invent a cold snap — and then the model would "discover" a forecast miss caused entirely by our own arithmetic.

### Verified live

```
Boston, MA     archive=360  forecast=312
Hartford, CT   archive=360  forecast=312
city rows: 1344  →  blended: 552 unique hours
hours not exactly 1h apart: 0        ← no gaps
source mix: archive 360 · mixed/forecast 192
```

672 city-hours per city → 552 unique after removing the 120-hour overlap. Correct, and reaching 2 days into the future.

---

## Step 5 — Ingest with idempotent upsert

`src/ingest.py`

**Why upsert rather than append.** The hourly job deliberately re-requests the **last 10 days**, because EIA back-fills and revises recent hours — a demand value published at T+1h is provisional and the settled value can differ. A blind append would create duplicate hours and double-count demand.

Upsert on `(period_utc, ba_code, series_type)` with `keep='last'` means the newest published value always wins, and **running the script twice produces exactly the same files as running it once**. That property — idempotence — is what makes an unattended pipeline safe to retry.

**Why Parquet for raw:** 1.1M rows is ~120 MB as CSV, ~8 MB as Parquet. That's the difference between a repo GitHub is happy with and one it isn't.

### Verified

```
36,288 rows landed across 3 month-partitions
freshness stamped · run ledger written · 86s
```

---

## Step 6 — SQL transformation layer

10 files in `sql/`. Key decisions:

**The dividing line between SQL and DAX** — be ready to defend this:

| | Goes in | Why |
|---|---|---|
| Row-level, deterministic | **SQL** | `ape`, degree hours, local hour, peak flag. One correct value per row regardless of what the user clicks. Compute once at build time |
| Aggregate, filter-dependent | **DAX** | MAPE, bias, YoY. Must recalculate on slicer change — a pre-computed "MAPE" column would be wrong the instant someone filtered to weekdays |

**`FULL OUTER JOIN`, not `INNER`.** The three horizons are different lengths — actual demand to ~1h ago, day-ahead forecast ~24–36h ahead, weather ~72h ahead. An `INNER JOIN` keeps only the overlap and silently discards the forward weather, which is precisely the data the peak-risk page needs. `row_kind` labels each row's nature instead.

**The rolling baseline excludes its own row.** `ROWS BETWEEN 720 PRECEDING AND 1 PRECEDING`. If the current hour were in its own baseline, an extreme hour would inflate the mean and standard deviation it's judged against and partly hide itself. That's data leakage.

### Verified — DuckDB ICU timezone handling

```
America/New_York  2026-07-15 18:00 UTC → 14:00 local (-04:00, DST)
America/New_York  2026-01-15 18:00 UTC → 13:00 local (-05:00, standard)
```

DST handled correctly. Fixed-offset arithmetic would be wrong for half the year.

---

## The bugs

### Bug 1 — A semicolon inside a comment shattered the SQL

**Symptom:** `Parser Error: syntax error at or near "if"` — DuckDB was trying to parse English prose.

**Cause:** `transform.py` split files on `;`. A header comment in `04_fact_load_hour.sql` contained *"...belongs in a different table; if a join could produce two rows..."*. The naive split chopped the file mid-sentence.

**Fix:** wrote a proper scanner that tracks two states — inside a single-quoted string literal, and inside a `--` line comment — and only treats `;` as a terminator when in neither. It also handles `''` as an escaped quote, which matters because the narrative SQL uses `printf('%02d:00', ...)`.

**The lesson:** naive string splitting on a structured format works right up until the content contains the delimiter. It always eventually does.

### Bug 2 — A SELECT referencing its own alias

**Symptom:** `Binder Error: Values list "b" does not have a column named "peak_vs_baseline_pct"`.

**Cause:** the narrative `CASE` referenced `peak_vs_baseline_pct`, a column being created in the *same* `SELECT` list.

**Fix:** added a `compared` CTE that materialises the comparison columns one step earlier.

**The lesson:** a `SELECT` list can't reliably reuse its own aliases in portable SQL. Splitting into named CTE stages is also simply easier to debug — each stage can be `SELECT`ed alone.

### Bug 3 — `ORDER BY` on a `UNION ALL`

**Symptom:** `Could not ORDER BY column "CASE WHEN severity = 'FAIL'..."`.

**Cause:** SQL doesn't allow `ORDER BY` on an expression absent from a set operation's `SELECT` list.

**Fix:** wrapped the `UNION ALL` block in a subquery.

### Bug 4 — A confident, meaningless number *(the most instructive one)*

**Symptom:** the generated brief announced *"Peak load 39,124 MW at 12:00, **98% above baseline**"* — for a normal Saturday.

**Cause:** on day five of the dataset, that Saturday's "weekend baseline" was an average over **a single prior Sunday**. The window function computed an average, nothing was `NULL`, nothing errored. The number was simply meaningless.

**Fix:** added `COUNT(...) OVER (same window)` and suppressed every baseline comparison below 4 observations, returning `NULL` instead.

**Why this is the most important bug here.** The first three were *loud* — the build stopped. This one was **silent and plausible**. It would have shipped, and a reader would have believed it.

> An average over a window is only as trustworthy as the number of observations in it, and a window function will never tell you that count unless you ask. **Always count the baseline alongside the baseline.**

`NULL` is a valid answer. "We don't know yet" beats a confident wrong number.

### Bug 5 — Comparing an unfinished day against a finished one

**Symptom:** the brief for the current day reported *"Peak load 75,483 MW at 08:00, **39% below baseline**"*.

**Cause:** the day was 9 hours old. Its peak hadn't happened yet. The "peak" was a running maximum of an in-progress day being compared against full-day baselines. That's not a finding, it's a clock.

**Fix:** added `is_complete_day` (≥20 hours published — 20 rather than 24 tolerates DST days and minor gaps), gated all peak comparisons on it, and adapted the narrative wording: an incomplete day now reports *"Highest load so far ... (day in progress, 9 of 24 hours published)"* and no baseline comparison at all.

**The generalisable lesson — and the reason this is worth its own entry:** MAPE *keeps* its comparison on a partial day, and correctly so. Verified:

| | days | peak comparisons | MAPE comparisons |
|---|---:|---:|---:|
| Partial | 24 | **0** | 6 |
| Complete | 360 | 312 | 312 |

> **Means degrade gracefully over incomplete data. Extremes do not.**
> A mean over 9 of 24 hours is a real, interpretable number. A *maximum* over 9 of 24 hours is just "the largest so far" wearing a peak's name. Knowing which aggregates survive partial data is a distinction worth carrying into any time-series work.

Bugs 4 and 5 are the same species: an aggregate that computed successfully, returned no error and no null, and meant nothing. Those are the ones that ship.

---

## Step 7 — Build verification

```
[PASS] fact_grain_unique           9,072 = 9,072 distinct keys
[PASS] fact_ba_orphans             0
[PASS] fact_date_orphans           0
[PASS] date_dim_contiguous         71 = 71
[PASS] demand_non_negative         0
[PASS] error_arithmetic_consistent 0
[WARN] no_demo_data                9,072   ← guard correctly caught the fixture
[WARN] data_freshness_hours        25      ← fixture was a day old
[PASS] ba_day_hour_coverage        0
[PASS] forecast_coverage           0
[PASS] mape_plausible              225 bps ← 2.25%, realistic
[PASS] weather_join_coverage       0
```

All six hard checks pass. Both warnings are **correct behaviour**, not defects.

### The analytical result, confirmed in the data

| Cooling degree hours | MAPE | Bias |
|---|---|---|
| 0 (none) | 2.12% | +0.17% |
| 0–5 | 2.09% | 0.00% |
| 5–10 | 1.84% | −0.12% |
| 10–15 | 1.62% | −0.06% |
| **15+** | **2.98%** | **+2.94%** |

Bias flips from ~zero to +2.94%. Not just noisier when hot — **directionally wrong**.

Hour-of-day, local time: evening peak worst at 2.66% MAPE, +1.88% bias.

Timezone conversion confirmed across four zones from one UTC hour: CISO 06:00, ERCO/MISO 08:00, ISNE/NYIS/PJM 09:00.

---

## Step 8 — Website

`src/build_web_data.py` → 9 JSON files, **651 KB total**.

**Columnar, not array-of-objects.** `{col: [values]}` repeats each key once instead of once per row — the same insight that makes Parquet fast, applied to JSON. Roughly 4× smaller, and charting libraries want columns anyway.

**No framework, deliberately.** Three static files. A React build would add a toolchain, a lockfile and a deploy step to a page that renders eight charts — and give a dependency release the power to break the site at 3am.

### Verified in a real browser

```
boot hidden · app visible · demo banner shown correctly
6 KPI cards · 5 narratives · 19 alert rows · 6 scorecard rows
12 DQ rows · 18 outlook rows · 0 console errors
```

**One real bug caught here:** charts on inactive tabs rendered at `0×0`. Chart.js can't measure a `display:none` canvas. Fixed by calling `.resize()` on every chart when a tab is revealed — confirmed working (`chartMapeTrend: 1389×475` after switching).

**Also fixed:** `DATE` columns serialise as `2026-06-28T00:00:00`. Trimmed by **string slicing**, not date parsing — `new Date('2026-06-28')` parses as UTC midnight and renders as the *previous day* for anyone west of Greenwich.

---

## Step 9 — Power BI + automation

**The `RelativePath` trap.** String-concatenated URLs are treated by Power BI Service as "dynamic data sources" and **cannot be scheduled for refresh**. Works perfectly in Desktop, fails in the cloud — the worst failure mode, because you don't find out until the data is stale.

```
Web.Contents( Base & "/path" )                        ✗ breaks cloud refresh
Web.Contents( Base, [RelativePath = "path"] )         ✓
```

**Cron at `:12`, not `:00`.** GitHub queues an enormous number of jobs at the top of the hour and scheduled workflows are best-effort — a `:00` job can be delayed 10–20 minutes or dropped. Choosing an off-peak minute is free.

**`git diff --staged --quiet` guard.** Without it the job fails every quiet hour when EIA has published nothing new. A workflow that "fails" routinely is one whose failures you stop reading.

---

## Open items

- [ ] Register EIA key, run `--probe`, commit the metadata
- [ ] Run `--backfill` for the full 25 months
- [ ] Confirm real-data MAPE lands in the 1.5–4% band
- [ ] Build the Power BI report from the spec
- [ ] Push, enable Pages, verify the first cron fires
- [ ] Screenshots into `screenshots/`, link them in the README
