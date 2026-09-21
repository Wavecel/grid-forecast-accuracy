# Data Sources

## 1. EIA API v2 — the grid data

**Base:** `https://api.eia.gov/v2`
**Key:** free, instant, no approval — https://www.eia.gov/opendata/register.php
**Licence:** US federal government work, **public domain**
**Docs:** https://www.eia.gov/opendata/documentation.php

### Why this source specifically

EIA is one of very few free datasets that publishes **both a prediction and the outcome it was predicting**, for the same hour and region:

| Facet `type` | Series | Role |
|---|---|---|
| `D` | Demand | The outcome |
| `DF` | Day-ahead demand forecast | **The prediction being graded** |
| `NG` | Net generation | In-footprint supply |
| `TI` | Total interchange | Net exports (+) / imports (−) |

Most open data gives you outcomes only. You can describe what happened, but never grade anyone's decision. `DF` is what makes this project possible.

### Routes used

| Route | Grain | Purpose |
|---|---|---|
| `electricity/rto/region-data` | BA × hour × type | Core series |
| `electricity/rto/fuel-type-data` | BA × hour × fuel | Renewable share KPI |
| `electricity/rto/region-data/facet/respondent` | — | Metadata probe |

### Query shape

EIA v2 uses PHP-style bracket arrays, which is unusual enough to be worth knowing:

```
GET /v2/electricity/rto/region-data/data/
    ?api_key=<KEY>
    &frequency=hourly
    &data[0]=value
    &facets[respondent][]=PJM
    &facets[type][]=D&facets[type][]=DF
    &start=2024-06-01T00&end=2024-07-01T00
    &sort[0][column]=period&sort[0][direction]=asc
    &offset=0&length=5000
```

Response:

```json
{ "response": { "total": "17856",
                "data": [ { "period": "2024-06-01T00", "respondent": "PJM",
                            "type": "D", "value": 95213,
                            "value-units": "megawatthours" } ] } }
```

### Three traps

**1. `length` caps at 5000.** Ask for more and you get 5000 **with no warning** — every row past that is silently lost. Six BAs × 4 series × 744 hours = ~17,856 rows in a long month, so a naive single request loses 70% of the data and looks like it worked.
*Handling:* page against `total` as ground truth, chunk by month, log a warning on any shortfall.

**2. `period` is UTC.** Treat it as local and every hour-of-day finding shifts 4–8 hours.
*Handling:* keep UTC in raw; convert once in SQL via ICU `AT TIME ZONE`.

**3. Recent hours are revised.** A value published at T+1h is provisional.
*Handling:* re-pull a 10-day window hourly and upsert on the natural key.

### Balancing authorities in scope

| Code | Name | Timezone | Why included |
|---|---|---|---|
| `PJM` | PJM Interconnection | America/New_York | Largest US market, stable benchmark |
| `MISO` | Midcontinent ISO | America/Chicago | Sprawling — hard weather proxy, deliberately |
| `ERCO` | ERCOT | America/Chicago | Extreme cooling load, scarcity events |
| `CISO` | California ISO | America/Los_Angeles | Duck curve, steep evening ramps |
| `ISNE` | ISO New England | America/New_York | Small, weather-whippy, dual peak |
| `NYIS` | New York ISO | America/New_York | Dense urban, sharp summer peaks |

**Rate limits:** generous (thousands/hour). This pipeline makes ~60 per hourly run.

### History available

`region-data` reaches back to **July 2015**. We take 25 months — enough for year-over-year, small enough to keep the repo light.

---

## 2. Open-Meteo — the weather driver

**Forecast:** `https://api.open-meteo.com/v1/forecast`
**Archive:** `https://archive-api.open-meteo.com/v1/archive`
**Key:** none. No account, no registration.
**Licence:** CC BY 4.0, free for non-commercial use
**Limit:** ~10,000 calls/day for non-commercial. We make ~62 per run.

### Why weather is central, not decoration

Roughly 70–90% of short-run variance in electricity demand is temperature. A load forecast is, at heart, a weather forecast plus a behavioural model.

Without weather you can say *"the forecast was wrong."* That is not an insight. With weather you can say *"it under-predicted by 4.1% on the first three days of the heat wave, when cooling load rose 38 MW per degree above 85°F."* That is something a modelling team can act on.

### The two-endpoint seam

```
[ -------------- archive (ERA5) -------------- ][ overlap ][ -- forecast -- ]
        reaches back decades, lags ~5 days       ↑ de-dup    past 10d + 3d ahead
```

Neither endpoint alone covers "25 months ago → tomorrow". **This is the most common weather-pipeline bug**: get it wrong and you get a silent 5-day hole in the *most recent* data — exactly the window the daily brief depends on. It's invisible until someone asks why yesterday is blank.

*Handling:* request generous overlap deliberately; de-duplicate on `(ba, city, hour)` preferring `archive` (exploiting `'archive' < 'forecast'` alphabetically, so a plain ascending sort does it).

### Variables pulled

`temperature_2m`, `relative_humidity_2m`, `wind_speed_10m`, `cloud_cover` — hourly, °F, UTC.

### The BA weather proxy

A balancing authority covers a huge area; no single city represents it. Each BA is a **population-weighted blend of 4–6 major load centres**, weights declared in `src/config.py` so they can be criticised openly.

| BA | Cities |
|---|---|
| PJM | Chicago .24, Philadelphia .20, Washington .18, Baltimore .14, Pittsburgh .12, Richmond .12 |
| MISO | Detroit .24, Minneapolis .22, St. Louis .16, Indianapolis .14, New Orleans .12, Des Moines .12 |
| ERCO | Houston .33, Dallas .30, Austin .19, San Antonio .18 |
| CISO | Los Angeles .42, San Francisco .16, San Diego .16, Fresno .13, Sacramento .13 |
| ISNE | Boston .45, Hartford .20, Providence .15, Manchester .10, Portland .10 |
| NYIS | New York .62, Buffalo .12, Albany .10, Rochester .09, Syracuse .07 |

**The renormalisation detail.** The weighted mean divides by the weight of cities that *actually reported*, not by full declared weight. If Fresno has a gap, a fixed denominator would drag California's blended temperature toward zero and manufacture a cold snap — and the model would then "discover" a forecast miss caused entirely by our own arithmetic.

### Derived: degree hours

```
cooling_degree_hours = max(temp°F − 65, 0)
heating_degree_hours = max(65 − temp°F, 0)
```

65°F is the long-standing US utility convention for the temperature at which neither heating nor cooling load is triggered. Degree-hours are the industry-standard weather-normalisation unit.

---

## 3. US federal holidays

From `pandas.tseries.holiday.USFederalHolidayCalendar`. No API, no cost.

**Why it matters more than it looks.** Holiday load behaves like a Sunday even on a Tuesday. Forecast models handle this badly because there are only ~10 examples per year to learn from, so holidays are reliably among the worst forecast days. Without this table every holiday would be misclassified as a weekday — polluting the weekday baseline, making normal weekdays look worse, and hiding the real holiday problem.

---

## What breaks if a source changes

| Change | Detection | Impact |
|---|---|---|
| EIA renames a facet | `--probe` diff in git | Ingest fails loudly |
| EIA drops `DF` for a BA | `forecast_coverage` DQ check | That BA becomes ungradeable |
| EIA changes `period` format | Parse guard drops rows + warns | Loud |
| Open-Meteo changes variable names | Column becomes NULL | `weather_join_coverage` WARN |
| Either goes down | HTTP retry exhausts, run logged FAILED | GitHub emails you; site serves last good commit |
| Row cap changes | Paging uses `total`, not a constant | Self-adjusting |
