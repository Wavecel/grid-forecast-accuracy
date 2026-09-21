# Setup & Deployment Runbook

Follow this top to bottom. Every step says **what** you're doing and **why**.
Total time: about 3 hours, most of it unattended.

---

## Phase 0 — What you need before starting

| Thing | Cost | Time | Notes |
|---|---|---|---|
| Python 3.11+ | Free | — | Already installed on your machine (3.13.3) |
| Git | Free | — | Already installed (2.49.0) |
| GitHub account | Free | 2 min | The repo must be **public** — this matters, see below |
| EIA API key | Free | 2 min | Instant, no approval step |
| Power BI Desktop | Free | 15 min | Windows only |
| Power BI account | Free | 5 min | Requires a **work/school** email — your `.edu` works |

**Why the repo must be public.** Three separate things depend on it:
1. `raw.githubusercontent.com` returns 404 for private repos without a token — Power BI could not read the data.
2. GitHub Pages is free only for public repos on the free plan.
3. GitHub Actions minutes are unlimited for public repos, metered for private.

A public repo is also the point — it's your portfolio.

---

## Phase 1 — Get the EIA API key

1. Go to **https://www.eia.gov/opendata/register.php**
2. Enter your email. The key arrives immediately on-screen and by email.
3. It looks like a 40-character alphanumeric string.

**Why EIA specifically:** it is one of very few free sources publishing both a *prediction* (`DF`, day-ahead demand forecast) and the *outcome* it was predicting (`D`, actual demand), for the same hour and the same region. That pairing is what makes this project possible at all. Most open data gives you outcomes only, which means you can describe what happened but never grade anyone's decision.

**Limits:** the documented cap is generous (thousands of requests/hour) and this pipeline makes about 60 per hourly run. You will not get close.

### Store the key locally

Create a file called `.env` in the project root:

```bash
echo "EIA_API_KEY=paste_your_key_here" > .env
```

`.env` is in `.gitignore`. **Never commit an API key** — GitHub scans public repos for leaked credentials, and EIA will revoke a key that appears in a public commit. If you leak one, revoke and reissue rather than deleting the commit; git history keeps it.

### Verify the key and probe the API

```bash
python -m src.ingest --probe
```

This writes `docs/api_metadata/eia_api_metadata.json` and confirms all six balancing authority codes exist. **Why probe first:** it validates your key, confirms the route and facet names, and records what the API looks like today. Commit that file — when EIA changes something, the diff shows up in git instead of appearing as a mysterious pipeline failure.

If it reports missing BA codes, open the JSON, find the correct codes, and update `BALANCING_AUTHORITIES` in `src/config.py`.

---

## Phase 2 — Backfill the history

```bash
python -m src.ingest --backfill
```

**Runtime:** roughly 5–10 minutes. It pulls 25 months of hourly data for 6 BAs across 4 series, plus 25 months of weather for 31 cities.

**Why 25 months and not 12:** year-over-year DAX measures need a full prior year *plus* the current partial year to compare against. At 12 months, `SAMEPERIODLASTYEAR` returns blank for every date and half your measure library is dead on arrival.

**Why the weather part is fast:** the Open-Meteo archive endpoint accepts a multi-year date range in a single request, so 31 cities is 31 calls, not 31 × 750 days.

Watch the log for `retrieved N of M promised rows` warnings — that's the paging guard telling you EIA truncated something.

---

## Phase 3 — Build the star schema

```bash
python -m src.transform
```

**Runtime:** under a minute.

This runs the ten SQL files in order against the raw Parquet, then runs 12 data quality checks. **A hard `FAIL` stops the build.** That's deliberate — a pipeline that publishes broken data is worse than one that stops, because a stopped pipeline gets fixed and a broken one gets believed.

Expected output: all six `FAIL`-severity checks PASS. The `no_demo_data` warning should now be gone (it only fires on the synthetic fixture).

Then:

```bash
python -m src.build_web_data
```

Produces roughly 650 KB of JSON in `web/data/`.

---

## Phase 4 — Preview the website locally

```bash
python -m http.server 8823 --directory web
```

Open **http://localhost:8823**.

**Why you must serve it over HTTP rather than double-clicking `index.html`:** browsers block `fetch()` on `file://` URLs for security reasons. The page would load and every chart would be empty. This trips up almost everyone the first time.

Check: the demo banner should be **gone**, the freshness dot green, and all five tabs populated.

---

## Phase 5 — Push to GitHub

```bash
git init
git add .
git commit -m "Grid load forecast accuracy: pipeline, model and dashboard"
git branch -M main
git remote add origin https://github.com/<YOUR-USERNAME>/grid-forecast-accuracy.git
git push -u origin main
```

### Add the API key as a repository secret

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`
- Name: `EIA_API_KEY`
- Value: your key

**Why a secret rather than a file:** Actions injects it as an environment variable at runtime and masks it in logs. Even if something tried to print it, the log shows `***`.

### Turn on GitHub Pages

`Settings` → `Pages` → Source: **GitHub Actions** (not "Deploy from a branch" — that's the older mechanism and ignores our workflow).

Your site goes live at `https://<username>.github.io/grid-forecast-accuracy/` within about two minutes.

### Verify the automation

`Actions` tab → `Hourly data refresh` → `Run workflow`. Watch it run. It should ingest, transform, build, and commit in about 3 minutes.

The cron (`12 * * * *`) then takes over. **Note:** GitHub disables scheduled workflows on repos with 60 days of no activity. The hourly commits keep it alive automatically.

---

## Phase 6 — Build the Power BI report

### 6.1 Connect the data

1. Open Power BI Desktop → **Transform data**
2. Create the two parameters from `powerbi/power_query/00_README_AND_PARAMETERS.m`:
   - `GitHubBaseUrl` = `https://raw.githubusercontent.com/<user>/grid-forecast-accuracy/main` (**no trailing slash**)
   - `UseParquet` = `true`
3. New Blank Query → paste `01_fnGetTable.m` → rename to `fnGetTable` → **uncheck Enable load**
4. Create eight more blank queries from `02_tables.m`, one per table
5. Close & Apply

**The single most important detail in this phase:** every query uses `Web.Contents(BaseUrl, [RelativePath = "..."])` rather than string concatenation. String-concatenated URLs are treated by Power BI Service as "dynamic data sources" and **cannot be scheduled for refresh**. It works perfectly on your desktop and fails in the cloud — the worst kind of bug, because you don't find out until the data goes stale.

### 6.2 Set up the model

1. **Mark the date table**: select `dim_date` → Table tools → *Mark as date table* → `date_key`.
   Skip this and time intelligence returns **wrong answers without erroring**.
2. Build relationships (all one-to-many, single direction, from dim to fact) — see `docs/04_data_model.md`.
3. Set `dim_hour[day_period]` to *Sort by column* → `day_period_sort`.
4. Set `dim_date[month_name]` to *Sort by column* → `year_month`.
5. Hide foreign keys on the fact tables from Report view.

### 6.3 Add the measures

Paste from `powerbi/dax/measures.dax`, one at a time via Modeling → New Measure. Assign each to its display folder as marked.

Start with these five and confirm they return sensible numbers before adding the rest:
`MAPE`, `Forecast Bias`, `Peak Hour MAPE`, `Gradeable Hours`, `Avg Demand MW`.

**Sanity check:** `MAPE` should land between 1.5% and 4%. If it's 0.02 you have a formatting issue; if it's 40% you likely have a timezone misalignment or actual and forecast swapped.

### 6.4 Build the pages

Follow `docs/06_dashboard_spec.md`.

---

## Phase 7 — Publish the report

### The licensing reality, stated plainly

| What you want | Power BI **Free** | Needs Pro ($14/user/mo) |
|---|---|---|
| Build in Desktop | Yes | — |
| Publish to *My Workspace* | Yes | — |
| Scheduled refresh, 8×/day | Yes | — |
| **Share a link with someone** | **No** | Yes |
| Publish to web (public embed) | Tenant-dependent | — |

So: **automated refresh is genuinely $0**, but "send my manager a link" is not.

For a portfolio this is fine, and there are two paths:

**Path A — Publish to web (public embed).** File → Publish to web. Gives you an iframe you can drop into `web/index.html`. **Caveat:** your university tenant admin may have disabled this — check `Settings → Admin portal → Tenant settings → Publish to web`. Also note that Publish to web is genuinely public: anyone with the URL can view it. Never use it for real company data.

**Path B — The site you already have.** The GitHub Pages dashboard is a complete public deliverable that depends on no tenant setting, plus screenshots and a screen recording of the Power BI report in the README.

**Recommendation: do Path B first**, since it's already working and can't be blocked. Add the Path A embed if your tenant allows it.

### Scheduled refresh

Power BI Service → your dataset → Settings → Scheduled refresh → up to 8 times/day on free.

**Why 8 is enough** even though data updates hourly: the *website* is hourly. Power BI is the analytical deep-dive, and a dataset refreshed every 3 hours is entirely adequate for that job. Match refresh frequency to the decision cadence, not to the data's maximum rate — over-refreshing is a cost with no benefit.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Charts empty, page loads | Opened `index.html` from disk | Serve over HTTP |
| `EIA_API_KEY is not set` | No `.env`, or key not exported | Create `.env` in the project root |
| `non-retryable HTTP 403` | Bad or revoked key | Re-register at eia.gov |
| Power BI: "can't schedule refresh" | Dynamic data source | Use `RelativePath`, not `&` |
| `Parquet.Document` not recognised | Old Power BI Desktop | Update, or set `UseParquet = false` |
| MAPE looks ~10× too small | Future rows in the denominator | Every error measure needs `row_kind = "actual"` |
| Actions cron stopped firing | 60 days inactivity | Push any commit |
| `fact_grain_unique` FAILs | Duplicate rows from a bad merge | Delete `data/raw/`, re-run backfill |
| Site shows the demo banner | Still on the fixture | Run `--backfill` with a real key |
