# Architecture

## The flow

```
┌──────────────────────┐        ┌──────────────────────┐
│   EIA API v2         │        │    Open-Meteo        │
│   hourly, free key   │        │    free, no key      │
│   D · DF · NG · TI   │        │  archive + forecast  │
└──────────┬───────────┘        └──────────┬───────────┘
           │  requests + retry/backoff/jitter          │
           └──────────────────┬───────────────────────┘
                              ▼
              ┌───────────────────────────────┐
              │  GitHub Actions  ·  cron :12  │   free (public repo)
              │  src/ingest.py                │
              └───────────────┬───────────────┘
                              ▼
              ┌───────────────────────────────┐
              │  data/raw/**.parquet          │   IMMUTABLE
              │  month-partitioned            │   idempotent upsert
              │  region_data · weather · fuel │   10-day revision window
              └───────────────┬───────────────┘
                              ▼
              ┌───────────────────────────────┐
              │  DuckDB  ·  sql/00-09         │   in-process
              │  staging → dims → facts → agg │   no server, no gateway
              │  12 DQ assertions             │   FAIL halts the build
              └───────────────┬───────────────┘
                              ▼
              ┌───────────────┴───────────────┐
              ▼                               ▼
  ┌────────────────────────┐      ┌────────────────────────┐
  │ data/curated/*.parquet │      │ web/data/*.json        │
  │ + *.csv                │      │ columnar · 630 KB      │
  └───────────┬────────────┘      └───────────┬────────────┘
              │ HTTPS, Web connector          │ git push
              ▼                               ▼
  ┌────────────────────────┐      ┌────────────────────────┐
  │  Power BI Import mode  │      │  GitHub Pages          │
  │  8 refreshes/day free  │      │  static, hourly        │
  │  45 DAX measures       │      │  no build step         │
  └───────────┬────────────┘      └───────────┬────────────┘
              ▼                               ▼
     Analytical deep-dive              Public dashboard
              └───────────────┬───────────────┘
                              ▼
                     Business decision
```

## Decisions and alternatives rejected

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| SQL engine | DuckDB | Supabase / Neon Postgres | **Power BI Service can't schedule refresh against cloud Postgres without an on-prem gateway** on an always-on machine. Not $0, not demoable |
| Storage | Parquet in Git | S3 / R2 | Free, versioned, and readable by Power BI over HTTPS with no credentials. Ceiling ~1 GB |
| Orchestration | GitHub Actions | Airflow / Prefect Cloud | Free and unlimited for public repos. Airflow needs a host |
| Power BI connection | Web + Parquet | Database connector | No gateway required. Works on the free licence |
| Raw format | Parquet | CSV | 1.1M rows: ~8 MB vs ~120 MB |
| Curated format | Both | One | Parquet for Power BI, CSV for humans and fallback |
| Web stack | Vanilla JS | React / Next | No toolchain, no lockfile, nothing to break at 3am. Eight charts don't need a framework |
| Web payload | Columnar JSON | Array-of-objects | Keys stored once, not per row. ~4× smaller |
| Transform location | SQL | pandas | Window functions, portability, and it's the skill being demonstrated |

## The three seams that actually matter

**1. Raw ↔ curated.** Nothing is transformed on the way in. A transform bug is a replay, not a re-download from a rate-limited API. The API is the one thing you can't get back.

**2. SQL ↔ DAX.** Row-level deterministic values (`ape`, degree hours, local hour, peak flag) are computed once in SQL. Aggregates that must respond to slicers (MAPE, bias, YoY) are DAX. A pre-computed MAPE *column* would be wrong the instant someone filtered to weekdays.

**3. Power BI ↔ website.** Same pipeline, two consumers with different constraints. Power BI gets the full 110k-row grain; the browser gets ~630 KB of pre-shaped aggregates. Serving one dataset to both would make one of them bad.

## Failure modes

| Failure | Detection | Effect | Recovery |
|---|---|---|---|
| API transient error | Retry exhausts, run logged FAILED | Job fails | GitHub emails; next hour retries |
| API schema change | `--probe` git diff; typed queries fail | Loud failure | Update config/SQL |
| Missed cron run | Freshness KPI on both surfaces | Slight staleness | Self-heals — 10-day re-pull |
| Bad data published upstream | 12 DQ assertions | **Build halts** | Site serves last good commit |
| Transform bug | DQ `FAIL` gate | Build halts | Fix SQL, replay from raw |
| Actions disabled (60d idle) | No commits appearing | Data freezes | Push any commit |
| Power BI refresh fails | Service email | Report stale, site fine | Usually the `RelativePath` issue |

**The design principle underneath all of these:** a stopped pipeline gets fixed; a broken one gets believed. Every gate is set to halt rather than publish.
