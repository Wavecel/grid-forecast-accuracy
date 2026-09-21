# Data Model

## The schema

```
                    ┌──────────────────┐
                    │     dim_ba       │
                    │──────────────────│
                    │ ba_code    (PK)  │
                    │ ba_name          │
                    │ market           │
                    │ region           │
                    │ timezone         │
                    │ size_band        │
                    │ peak_season      │
                    │ weather_cities   │
                    │ 6 rows           │
                    └────────┬─────────┘
                             │ 1
             ┌───────────────┼───────────────┬──────────────────┐
             │ *             │ *             │ *                │ *
   ┌─────────┴────────┐  ┌───┴──────────┐  ┌─┴──────────────┐   │
   │ fact_load_hour   │  │fact_fuel_mix │  │agg_daily_brief │   │
   │──────────────────│  │──────────────│  │────────────────│   │
   │ ba_code    (FK)  │  │ ba_code (FK) │  │ ba_code   (FK) │   │
   │ period_utc       │  │ period_utc   │  │ date_key  (FK) │   │
   │ date_key   (FK)  │  │ date_key(FK) │  │ mape           │   │
   │ local_hour (FK)  │  │ local_hour   │  │ bias_pct       │   │
   │ row_kind         │  │ fuel_code(FK)│  │ status_band    │   │
   │ demand_mw        │  │ generation_mw│  │ daily_narrative│   │
   │ forecast_mw      │  │ gen_share    │  │ ~4,500 rows    │   │
   │ forecast_error_mw│  │ ~880k rows   │  └───────┬────────┘   │
   │ ape / signed_pe  │  └───┬──────┬───┘          │            │
   │ temp_f, CDH, HDH │      │      │ *            │            │
   │ ape_zscore       │      │   ┌──┴─────────┐    │            │
   │ ~110,000 rows    │      │   │  dim_fuel  │    │            │
   └────┬────────┬────┘      │   │ fuel_code  │    │            │
        │ *      │ *         │ * │ 8 rows     │    │ *          │
   ┌────┴────────┴───────────┴───┴────────────┴────┴────────────┴──┐
   │                          dim_date                              │
   │  date_key (PK) · year · month · season · day_type · is_holiday │
   │  ~800 rows, CONTIGUOUS                                         │
   └────────────────────────────────────────────────────────────────┘

   ┌──────────────┐            ┌──────────────┐
   │  dim_hour    │            │  dq_checks   │
   │ hour_of_day  │────────────│ (standalone) │
   │ day_period   │  1  →  *   │  12 rows     │
   │ 24 rows      │  to facts  └──────────────┘
   └──────────────┘
```

## Relationships to create

| From (one) | To (many) | Cardinality | Direction | Active |
|---|---|---|---|---|
| `dim_ba[ba_code]` | `fact_load_hour[ba_code]` | 1:* | Single | Yes |
| `dim_ba[ba_code]` | `fact_fuel_mix[ba_code]` | 1:* | Single | Yes |
| `dim_ba[ba_code]` | `agg_daily_brief[ba_code]` | 1:* | Single | Yes |
| `dim_date[date_key]` | `fact_load_hour[date_key]` | 1:* | Single | Yes |
| `dim_date[date_key]` | `fact_fuel_mix[date_key]` | 1:* | Single | Yes |
| `dim_date[date_key]` | `agg_daily_brief[date_key]` | 1:* | Single | Yes |
| `dim_hour[hour_of_day]` | `fact_load_hour[local_hour]` | 1:* | Single | Yes |
| `dim_hour[hour_of_day]` | `fact_fuel_mix[local_hour]` | 1:* | Single | Yes |
| `dim_fuel[fuel_code]` | `fact_fuel_mix[fuel_code]` | 1:* | Single | Yes |

`dq_checks` stays unrelated — it describes the *pipeline*, not the *data*, so it has no meaningful key into the facts.

---

## Design decisions, and why

### Why a star schema rather than one flat table

A single wide table would work and would even be fast. It is rejected for reasons that show up later:

- **Slicers need dimensions.** A "Region" slicer built on a fact column lists one entry per fact row internally; on a 6-row dimension it's trivial. On 110,000 rows the difference is real.
- **Repeated text kills compression.** VertiPaq compresses by column. `"ISO New England"` repeated 18,000 times compresses well but never as well as an integer key into a 6-row table.
- **Two facts need shared filters.** `fact_load_hour` and `fact_fuel_mix` have *different grains*. A single BA slicer must filter both consistently. That only works if they share a dimension — the property called a **conformed dimension**.

### Why single-direction relationships

Bidirectional cross-filtering is available and almost always a mistake. It creates ambiguous filter paths once you have two facts on a shared dimension, and DAX resolves ambiguity in ways that are hard to predict and harder to debug. Single direction (dim → fact) is the default for a reason. If you genuinely need reverse filtering in one measure, use `CROSSFILTER()` inside that measure rather than changing the model globally.

### Why `fact_fuel_mix` is a separate table

Grain. `fact_load_hour` is one row per **BA-hour**. Fuel mix is one row per **BA-hour-fuel**. Merging them would force either:

- pivoting fuel into ~8 columns (breaks the moment EIA adds a fuel type, and can't be sliced by a Fuel dimension), or
- fanning the load fact out 8×, which multiplies every demand total by 8.

That second one is the **fan trap**, and it's dangerous precisely because the resulting numbers look plausible. Nobody notices for a month.

### Why `agg_daily_brief` exists at all

Normally you should *not* pre-aggregate — DAX over the hourly fact is more flexible. Two specific reasons override that here:

1. **The website has no DAX engine.** It reads JSON. 4,500 daily rows is instant; 110,000 hourly rows is not.
2. **The narrative is a row-level string.** A generated sentence can't be produced by a measure that must respond to arbitrary filters.

### Why `row_kind` is the most important column in the model

The fact table holds three kinds of row, because the three data horizons have different lengths:

| `row_kind` | Meaning | Horizon |
|---|---|---|
| `actual` | Demand published — gradeable | up to ~1h ago |
| `forecast_only` | Forecast exists, actual doesn't yet | ~24–36h ahead |
| `weather_only` | Forward weather outlook only | ~72h ahead |

An `INNER JOIN` between grid and weather would keep only the overlap and silently discard the forward weather — the exact data the peak-risk page needs. So the SQL uses a `FULL OUTER JOIN` and labels each row.

**The cost of that choice:** every error measure must filter `row_kind = "actual"`. Forget it once and MAPE dilutes toward zero as non-gradeable rows enter the denominator. It won't error. It will look plausible. This is the single most likely way to break this model, which is why it's called out in the DAX file, the SQL, and here.

---

## Column reference — `fact_load_hour`

**Grain: one row per (balancing authority, UTC hour).**

| Column | Type | Meaning |
|---|---|---|
| `ba_code` | text | FK → `dim_ba` |
| `period_utc` | datetimezone | The hour, in UTC. The absolute instant |
| `local_datetime` | datetime | Same instant in the BA's local time, DST-aware |
| `date_key` | date | **Local** date. FK → `dim_date` |
| `local_hour` | int | 0–23 local. FK → `dim_hour` |
| `row_kind` | text | `actual` / `forecast_only` / `weather_only` |
| `day_type` | text | Weekday / Weekend / Holiday |
| `demand_mw` | number | Actual demand (EIA `D`) |
| `forecast_mw` | number | Day-ahead forecast (EIA `DF`) |
| `net_generation_mw` | number | In-footprint generation (EIA `NG`) |
| `interchange_mw` | number | Net exports +, imports − (EIA `TI`) |
| `forecast_error_mw` | number | `demand − forecast`. **+ = under-forecast, short** |
| `abs_error_mw` | number | Absolute error |
| `ape` | number | `abs_error / demand`. Ratio, not percent |
| `signed_pe` | number | `error / demand`. Signed |
| `ramp_mw` | number | `demand(h) − demand(h−1)` |
| `temp_f` | number | Population-weighted temperature |
| `cooling_degree_hours` | number | `max(temp − 65, 0)` |
| `heating_degree_hours` | number | `max(65 − temp, 0)` |
| `is_daily_peak_hour` | int | 1 if this hour is that local day's peak |
| `is_material_miss` | int | 1 if APE > 5% |
| `ape_zscore` | number | vs trailing 30d, **excluding this hour** |
| `weather_coverage` | number | Share of weather cities reporting |
| `is_demo_data` | int | 1 = synthetic fixture. Must be 0 to publish |

**On `ape` being a ratio, not a percentage:** store `0.0234`, format as `2.34%` in the visual. Storing `2.34` and calling it a percentage means every downstream calculation needs a `/100` that someone will eventually forget. Store the number, format at the edge.
