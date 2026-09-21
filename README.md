# Grid Load Forecast Accuracy

**An automated analytics pipeline that grades US day-ahead electricity load forecasts against what actually happened — and uses weather data to explain why they miss.**

[Live dashboard](https://YOUR-USERNAME.github.io/grid-forecast-accuracy/) · [Setup runbook](docs/07_setup_and_deployment.md) · [Data model](docs/04_data_model.md) · [Dashboard spec](docs/06_dashboard_spec.md)

Refreshes hourly. Runs at **$0/month**.

---

## The business problem

Every US balancing authority publishes a day-ahead forecast of how much electricity its region will need, then buys power against that number.

- Forecast **too low** → the operator is **short**, and must buy the shortfall on the real-time market, usually at a premium.
- Forecast **too high** → the operator is **long**, and sells the surplus back, often at a loss.

Both directions cost money. At PJM's scale, a 1% error is roughly **1,000 MW**.

Forecasting teams track error, but usually as a single monthly average. That number conflates two very different things:

| | What it is | Fixable? |
|---|---|---|
| **MAPE** | Noise — how imprecise the forecast is | Largely no |
| **Bias** | Systematic direction — consistently high or low | **Yes, by recalibration** |

**This project separates them, and identifies the conditions under which bias appears.** That turns "the forecast was off in July" into a specific, testable modelling claim.

**Who would use it:** load forecasting analyst, energy procurement manager, demand-response operations, or a retail energy supplier's trading desk.

---

## What the analysis finds

The core result, visible on the *Root Cause* page:

| Conditions | MAPE | Bias |
|---|---|---|
| Mild (< 5 cooling degree hours) | ~2.1% | **~0.0%** |
| Extreme heat (> 15 CDH) | ~3.0% | **+2.9%** |

In mild weather the forecast is essentially **unbiased** — noisy, but not wrong in a direction. Above roughly 15 cooling degree hours it becomes **directionally wrong**, consistently under-predicting load.

That asymmetry matters because it is *not* irreducible uncertainty. A forecast that is unbiased in mild weather and biased in heat has an **under-modelled air-conditioning response** — and it recurs on exactly the days when being wrong is most expensive.

> **A note on rigour:** this is a correlational finding at hourly grain and the dashboard wording is hedged accordingly. It identifies a pattern worth investigating, not a proven cause.

---

## Architecture

```
  EIA API v2                    Open-Meteo
  hourly demand (D)             hourly temperature
  day-ahead forecast (DF)       ERA5 archive + live forecast
  generation, interchange       free, no API key
        │                             │
        └──────────────┬──────────────┘
                       ▼
        GitHub Actions cron  ·  hourly  ·  free
                       ▼
        data/raw/**.parquet   ← IMMUTABLE, month-partitioned
                       ▼
        DuckDB  ·  10 SQL files  ·  in-process, no server
           star schema + 12 data-quality gates
                       ▼
        ┌──────────────┴───────────────┐
        ▼                              ▼
  data/curated/*.parquet         web/data/*.json
        ▼                              ▼
  Power BI (Import mode)         GitHub Pages
  read over HTTPS, no gateway    static, ~650 KB
        ▼                              ▼
   Analytical deep-dive          Public dashboard
```

### Three architectural decisions worth defending

**1. DuckDB, not a hosted Postgres.**
The obvious choice is a free-tier Supabase or Neon. It's the wrong one: **Power BI Service cannot schedule a refresh against cloud PostgreSQL without an on-premises data gateway** running on an always-on machine. That isn't $0, and you can't demo it from a laptop. DuckDB is in-process — no server, no credentials, no gateway — and the SQL (CTEs, window frames, `FULL OUTER JOIN`, conditional aggregation) ports to Postgres or Snowflake nearly unchanged.

**2. Raw is immutable and separate from curated.**
Nothing is transformed on the way in. If a transform has a bug you fix the SQL and replay — you don't re-download 25 months from a rate-limited API. The API is the one thing you can't get back.

**3. Parquet over HTTPS, not a database connection.**
Power BI's Web connector reads `raw.githubusercontent.com` with no gateway and no paid licence. Parquet is ~10× smaller than CSV and carries real data types, which eliminates an entire class of locale bug where the Service parses `03/04/2026` differently than your laptop does.

---

## Technical highlights

**SQL** — 10 files, ~700 lines
- Long→wide conditional pivot (`MAX(CASE WHEN ...)`, portable ANSI)
- `FULL OUTER JOIN` across three different data horizons, with an explicit `row_kind` label
- Window functions: `LAG`, partitioned `MAX`, and a rolling 30-day baseline
- **Rolling frame excludes the current row** (`ROWS BETWEEN 720 PRECEDING AND 1 PRECEDING`) so an anomaly can't contaminate its own benchmark — that's data leakage, and it's the difference between an anomaly detector that works and one that flatters itself
- Timezone-correct local time via ICU `AT TIME ZONE`, DST-aware
- 12 materialised data-quality assertions that **gate the build**

**Python** — ~1,400 lines
- Exponential backoff with jitter; retries 429/5xx, never retries 4xx
- Paging against EIA's `total` with explicit truncation warnings (the 5000-row cap silently drops rows otherwise)
- **Idempotent upsert** on a 10-day revision window — running twice produces the same files as running once
- API metadata probe committed to git, so schema drift appears as a **diff** rather than a mystery failure

**Power BI** — 45 measures
- `RelativePath` in every query (string-concatenated URLs are "dynamic data sources" and **cannot** be scheduled for refresh)
- Least-squares regression slope computed in pure DAX, with an `N > 30` guard
- Correlation coefficient in DAX
- Forecast skill score vs a naive persistence baseline
- Time intelligence, field parameters, what-if scenario, drill-through, dynamic titles

**Front end** — no build step, no framework
- Three static files + JSON. Nothing to keep patched, no toolchain to break at 3am
- Columnar JSON (`{col: [values]}` not `[{...}]`) — same idea as Parquet, ~4× smaller

---

## Cost

| Component | Tool | Monthly |
|---|---|---:|
| Data source | EIA API v2 | $0 |
| Weather | Open-Meteo | $0 |
| Orchestration | GitHub Actions (unlimited for public repos) | $0 |
| Storage | GitHub repo, Parquet | $0 |
| Transformation | DuckDB | $0 |
| Semantic model | Power BI Desktop + free licence | $0 |
| Web hosting | GitHub Pages | $0 |
| **Total** | | **$0** |

**Free-tier limits, stated honestly:**
- Power BI Free allows scheduled refresh in *My Workspace* (8×/day) but **cannot share a report link** — that needs Pro at $14/user/month.
- GitHub disables cron on repos idle 60 days. The hourly commits prevent this automatically.
- GitHub Actions cron is best-effort; jobs scheduled at `:00` are frequently delayed. This one runs at `:12`.

---

## Repository layout

```
├── src/                    Python pipeline
│   ├── config.py           Every assumption, in one auditable place
│   ├── utils.py            Retry, logging, run ledger
│   ├── eia_client.py       Paging + metadata probe
│   ├── weather_client.py   Archive/forecast stitching
│   ├── ingest.py           Raw landing, idempotent upsert
│   ├── transform.py        SQL runner + build gate
│   └── build_web_data.py   Compact JSON for the website
├── sql/                    10 numbered transformation files
├── powerbi/
│   ├── power_query/        M source
│   └── dax/measures.dax    45 measures, foldered
├── web/                    The public dashboard
├── data/
│   ├── raw/                Immutable, month-partitioned Parquet
│   └── curated/            Star schema — what Power BI reads
├── docs/                   Business case, model, spec, runbook, interview prep
└── .github/workflows/      Hourly refresh + Pages deploy
```

---

## Running it yourself

```bash
pip install -r requirements.txt
echo "EIA_API_KEY=your_key" > .env      # free: eia.gov/opendata/register.php
python -m src.ingest --probe            # verify the API contract
python -m src.ingest --backfill         # 25 months, ~5-10 min
python -m src.transform                 # star schema + quality gates
python -m src.build_web_data            # website payload
python -m http.server 8823 --directory web
```

No API key yet? `python -m src.ingest --demo` builds a synthetic fixture (real weather, modelled demand) so the whole stack runs immediately. Every row is stamped `is_demo_data=1`, a quality check flags it, and the website shows a banner.

Full instructions: [docs/07_setup_and_deployment.md](docs/07_setup_and_deployment.md).

---

## Limitations

- **Weather is a proxy.** Each BA is represented by a population-weighted blend of 4–6 cities, not true load-weighted weather. Understates variation in sprawling footprints, MISO especially.
- **Correlation, not causation.** Hourly-grain association is suggestive, not proof.
- **EIA revises.** Recent hours can change after first publication; the pipeline re-pulls a 10-day window and upserts.
- **No settlement prices.** Real imbalance costs aren't free data. Any dollar figure is an explicit user-set scenario, never a measured result.

Full discussion: [docs/limitations.md](docs/limitations.md).

---

## Data licensing

- **EIA** — US federal government work, public domain. [Terms](https://www.eia.gov/about/copyrights_reuse.php)
- **Open-Meteo** — CC BY 4.0, free for non-commercial use. [Terms](https://open-meteo.com/en/terms)

Not affiliated with any balancing authority, system operator, or the EIA.
