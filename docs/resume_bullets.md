# Resume Bullets

## The rule applied here

Every bullet is **Action + Technology + Analytical method + Business relevance.**

**No invented impact numbers.** You have not measured a dollar saving, because real imbalance settlement prices are not free data. Bullets that claim one are the fastest way to lose a technical interviewer — they will ask how you measured it, and there is no good answer.

What you *can* truthfully claim: what you built, what method you used, what you found, and what decision it supports. That is enough, and it reads as more credible than a fabricated percentage.

---

## Primary set — use these three

> **Built an automated analytics pipeline** (Python, DuckDB SQL, GitHub Actions) ingesting hourly grid demand and day-ahead forecast data from the US EIA API across six balancing authorities, joined to population-weighted weather from Open-Meteo — producing a refreshed star-schema dataset of ~110,000 hourly records at **$0 infrastructure cost**.

> **Designed a Power BI model with 45 DAX measures** including time intelligence, a least-squares regression slope, and forecast skill scoring against a naive persistence baseline — **separating irreducible forecast noise from systematic bias**, the distinction that determines whether a forecast error is actionable.

> **Identified that day-ahead load forecasts are unbiased in mild conditions but systematically under-predict demand above 15 cooling degree hours**, indicating an under-modelled air-conditioning response concentrated on peak-demand days; surfaced the finding through an automated daily brief with anomaly detection benchmarked against each operator's own 30-day error distribution.

---

## Alternates — swap by role emphasis

**Data engineering emphasis**
> Engineered an idempotent hourly ETL pipeline with exponential-backoff retry, API pagination against server-reported totals, and a 10-day upsert window handling upstream data revisions — gated by **12 automated data-quality assertions** that halt the build on referential-integrity, grain-uniqueness, or calendar-contiguity failures.

**SQL emphasis**
> Authored a 10-stage SQL transformation layer (CTEs, window functions, conditional pivots, timezone-aware `FULL OUTER JOIN` across three differing data horizons) building a star schema with two fact tables at distinct grains sharing conformed dimensions, including a **leakage-free rolling 30-day anomaly baseline** that excludes the observation being scored.

**Analytics / storytelling emphasis**
> Built a decision-support dashboard answering what changed, why, and what requires attention today — generating **automated daily narratives** that compare each day against a trailing 28-day baseline **of the same day type**, preventing normal weekday load patterns from being misread as forecast failures.

**Full-stack / product emphasis**
> Published a **public, hourly-refreshing web dashboard** (GitHub Pages, vanilla JS, ~650 KB columnar-JSON payload) alongside the Power BI model from a single automated pipeline — including a live data-quality page that surfaces the project's own validation checks and data freshness to viewers.

**Business analyst emphasis**
> Translated an operational cost problem (day-ahead load forecast error drives real-time imbalance purchases) into a measurable analytical framework, defining **KPIs distinguishing fixable systematic bias from irreducible noise** and delivering a ranked alert queue prioritised by statistical severity rather than a fixed error threshold.

---

## Skills line

```
Power BI (DAX, Power Query M, star schema, RLS-aware design) · SQL (window
functions, CTEs, dimensional modelling) · Python (pandas, API integration,
ETL) · DuckDB · Parquet · Git/GitHub Actions CI/CD · Data quality engineering ·
Time-series & anomaly analysis · Dashboard design & data storytelling
```

---

## Project header for the resume

> **Grid Load Forecast Accuracy** — Automated BI pipeline & dashboard
> *Python · SQL · DuckDB · Power BI · GitHub Actions* · [live dashboard](https://YOUR-USERNAME.github.io/grid-forecast-accuracy/) · [repo](https://github.com/YOUR-USERNAME/grid-forecast-accuracy)

---

## LinkedIn / portfolio summary (2–3 sentences)

> Every US grid operator publishes a day-ahead forecast of electricity demand and buys power against it — being wrong in either direction costs money, and at PJM's scale a 1% error is about 1,000 MW. I built an hourly-refreshing pipeline that grades those forecasts against actual outcomes and uses weather data to explain the misses, separating irreducible noise from systematic, fixable bias. The analysis found forecasts are essentially unbiased in mild weather but systematically under-predict load in extreme heat — a pattern that recurs on exactly the days when being wrong is most expensive.

---

## Bullets to avoid, and why

| Don't write | Why it fails |
|---|---|
| "Reduced forecast error by 15%" | You built no forecast. Instantly falsifiable |
| "Saved $2M in imbalance costs" | You have no settlement price data. First follow-up question kills it |
| "Analysed 1M+ records" | Row count is not an achievement. ~110k curated rows anyway — don't inflate |
| "Used advanced DAX" | Vague. Name the actual technique: regression slope, skill score |
| "Real-time dashboard" | It's hourly. Precision here is free credibility |
| "Machine learning model" | There is none. Regression slope ≠ ML, and claiming it invites questions you can't answer |

The through-line: **every claim must survive the follow-up question.**
