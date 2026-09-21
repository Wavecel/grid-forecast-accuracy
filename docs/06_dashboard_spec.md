# Power BI Dashboard Specification

Six pages. Each visual below states the business question it answers — if a visual can't answer one, it doesn't belong on the page.

**Canvas:** 1600 × 900, 16:9. **Theme:** dark. **Colour rules, applied consistently across all pages:**

| Colour | Meaning | Hex |
|---|---|---|
| Cyan | Actual / observed | `#38BDF8` |
| Amber | Forecast / predicted | `#FBBF24` |
| Red | Under-forecast (short) / Attention | `#F43F5E` |
| Blue | Over-forecast (long) | `#60A5FA` |
| Green | Normal | `#34D399` |

A colour that means nothing dilutes the ones that do. Never use red for "actual" on one page and "bad" on another.

---

## Page 1 — Daily Brief

**Audience:** Load forecasting manager, first thing in the morning.
**Question:** *"What changed since yesterday, and does anything need me today?"*

### Layout

**Top strip — dynamic title card**
- Visual: Card, title from `[Title Status Banner]`
- Answers: *Are we normal, watching, or in trouble right now?*

**KPI row — six cards**

| Card | Measure | Conditional format |
|---|---|---|
| MAPE (selected period) | `[MAPE]` | Background by `[Forecast Status Color]` |
| vs 28-day baseline | `[MAPE vs Baseline %]` | Red if > 0, green if < 0 |
| Systematic bias | `[Forecast Bias]` | Amber if `abs > 1%` |
| Peak-hour MAPE | `[Peak Hour MAPE]` | — |
| Material misses | `[Material Miss Hours]` | Red if above baseline |
| Anomaly hours (2σ) | `[Anomaly Hours 2sigma]` | Red if > 0 |

**Why bias gets equal billing with MAPE:** MAPE says the forecast is noisy — largely irreducible. Bias says it's systematically wrong in a direction — and that part is fixable by recalibration. Showing only MAPE hides the actionable half.

**Narrative card**
- Visual: Card (multi-row) bound to `[Daily Narrative]`
- Answers: *What happened, where, and what likely drove it* — in a sentence

**"Requires attention" table**
- Columns: BA, local time, actual, forecast, error MW, APE, z-score, direction
- Filter: `ape_zscore >= 2`, last 3 days
- Sort: z-score descending
- Conditional format: data bars on error MW, red/blue by sign
- **Drill-through target:** Page 4 (Hour Detail)
- Answers: *Which specific hours went wrong, worst first?*

**Why z-score rather than an absolute threshold:** 3% error is an alert for a normally-accurate BA and routine for a volatile one. A fixed threshold would permanently red-flag the small operators and green-light the large ones — and users would correctly stop trusting the colour.

**Actual vs forecast line chart**
- X: `local_datetime` (last 14 days), Y: `demand_mw` cyan solid, `forecast_mw` amber dashed
- Answers: *How closely is the forecast tracking?*

**Error column chart**
- X: `local_datetime`, Y: `forecast_error_mw`, colour by sign
- Answers: *Are we consistently short, consistently long, or just noisy?*

### Interactions
- **Slicers:** BA (dropdown), date range (relative, "Last 7 days" default)
- **Sync slicers** across pages 1–3 only. Page 5 (Data Quality) deliberately unsynced — you want to see pipeline health for everything at once.
- **Bookmarks:** "Last 7d" / "Last 30d" / "Last 90d" as a button group

---

## Page 2 — Forecast Accuracy

**Audience:** Forecasting analyst / manager doing a monthly review.
**Question:** *"Which operators are accurate, and is anyone trending worse?"*

**Headline insight card** — text box driven by measures, stating the current spread between best and worst BA.

**Daily MAPE trend**
- Line chart, X `dim_date[date_key]`, Y `[MAPE]`, legend `dim_ba[ba_code]`
- Add `[MAPE 7D Rolling]` as a second, thicker series
- **Analytics pane:** add a constant line at 3% labelled "typical industry band"
- Answers: *Is accuracy improving or degrading over time?*

**MAPE vs Bias combo**
- Clustered column (`[MAPE 30D Rolling]`) + line (`[Bias 30D Rolling]`) on a secondary axis, X = BA
- Answers: *How much of each operator's error is fixable?*

**Peak-hour vs all-hours**
- Clustered bar: `[MAPE]` and `[Peak Hour MAPE]` by BA
- Answers: *Does accuracy degrade exactly when it matters most?*

**Forecast skill score**
- Card + bar by BA: `[Forecast Skill Score]`
- Answers: *Is the day-ahead forecast actually beating a naive "same hour last week" guess?*
- **Why this is worth a visual:** it's the honest test. A forecast that can't beat persistence isn't adding value regardless of how good its MAPE looks alone.

**Scorecard matrix**
- Rows: BA. Values: MAPE 7d / 30d / all, bias, peak-hour MAPE, misses
- Conditional formatting: colour scale on MAPE columns

**Field parameter demo**
- One chart driven by a Fields parameter (`MAPE` / `Bias` / `Peak Hour MAPE` / `Material Miss Rate`)
- **Why it earns its place:** one chart answers four questions instead of four charts crowding the page. That's a genuine usability gain, not a feature added to look advanced.

---

## Page 3 — Root Cause (Why the forecast misses)

**Audience:** Forecasting modeller.
**Question:** *"Is this random noise, or is there structure we can model?"*

This is the analytical heart of the report.

**Bias vs cooling degree hours** *(the centrepiece)*
- Combo: column = `[Forecast Bias]` coloured by sign, line = `[MAPE]` on secondary axis
- X: `cooling_degree_hours` binned in 2.5-unit groups
- Answers: *Does the forecast fail differently when it's hot?*
- **The finding it exposes:** bias near zero in mild conditions, strongly positive above ~15 CDH. That asymmetry is an under-modelled air-conditioning response — a fixable modelling gap, not bad luck.

**Cooling sensitivity card**
- `[Cooling Sensitivity MW per CDH]` — a least-squares slope computed in DAX
- Answers: *How many extra MW does each cooling degree hour add?*
- Business use: translates a weather forecast into a capacity requirement.

**Error/temperature correlation card**
- `[Error Temp Correlation]`
- Answers: *Is there unexploited weather signal left in the forecast's residuals?*
- A materially non-zero value is the technical statement of "it isn't modelling heat properly".

**Hour-of-day profile**
- Line, X `dim_hour[hour_label]`, Y `[MAPE]`, legend BA
- **Uses local hour**, so four time zones are comparable
- Answers: *When in the day does error concentrate?*

**Load vs temperature scatter**
- X `temp_f`, Y `demand_mw`, colour by `day_type`, size by `abs_error_mw`
- Add a trend line from the Analytics pane
- Answers: *What is the load/temperature response curve, and where does it bend?*

**Day-type comparison**
- Bar: `[MAPE]` by `dim_date[day_type]`
- Answers: *Are holidays structurally harder?* (They are — load behaves like a Sunday on a Tuesday, and a model has ~10 examples a year to learn from.)

**Decomposition tree**
- Analyse `[Total Absolute Error MWh]`, explain by BA → season → day_type → `dim_hour[day_period]`
- Answers: *Where is total error concentrated?*
- **Why a decomposition tree here specifically:** the question is genuinely hierarchical and exploratory. Using one where a bar chart would do is the trap; this isn't that.

**Key influencers**
- Analyse `is_material_miss`, explain by `cooling_degree_hours`, `temp_change_1h`, `day_type`, `local_hour`, `ba_code`
- Answers: *What conditions make a material miss more likely?*
- **Caveat to state on the page:** this finds association, not causation.

---

## Page 4 — Hour Detail (drill-through target)

**Not in the page navigation.** Reached by right-clicking a row on Page 1 or 2.

**Drill-through filters:** `ba_code`, `date_key`

- Header card: BA, date, day type, holiday name
- Hourly table: every hour of that day — demand, forecast, error, APE, temp, CDH, ramp, peak flag
- Line: actual vs forecast for the 24 hours
- Column: hourly error
- Weather strip: temperature and CDH across the day
- Context card: `[MAPE]` for the day vs `[MAPE Baseline 28D]`

**Why a drill-through rather than another page:** it inherits filter context automatically, so it can't disagree with the page you came from. A separate page with its own slicers can and will drift out of sync, and the user won't notice.

---

## Page 5 — Forward Outlook

**Audience:** Operations planner.
**Question:** *"What should I be ready for in the next 72 hours?"*

- **Temperature outlook line:** forward `temp_f` vs a constant line at the trailing 30-day max, by BA
- **Risk table:** BA × date, max forecast temp, delta vs 30-day max, CDH, peak day-ahead forecast MW, risk band
- **Callout:** *"N BA-days forecast above their recent 30-day maximum"*
- **Explanatory text box** covering why forward rows exist at all (the `FULL OUTER JOIN` and the three horizons)

Bars above the line = conditions the load model is **extrapolating** into rather than interpolating within, which is historically where under-forecasting is worst.

---

## Page 6 — Data & Pipeline Health

**Audience:** You, in an interview, when asked "how would you know if this data were wrong?"

- **KPI cards:** checks passing, warnings, data lag hours, fact row count
- **DQ table:** all 12 checks with result, severity, observed, threshold, description
- **Coverage bar:** gradeable hours per BA
- **Freshness card:** `[Data Freshness Label]`
- **Run history:** from `run_log.csv` if loaded — success/failure over time
- **Text box:** the known limitations, verbatim from `docs/limitations.md`

**Why this page exists:** every number in the report rests on assumptions that can silently break. Publishing the checks alongside the results means a viewer never has to take the data on faith. Almost no portfolio project does this, and it pre-empts the sharpest question a reviewer will ask.

---

## Advanced features — where each is *justified*

| Feature | Where | Why it earns its place |
|---|---|---|
| Drill-through | Alert table → Page 4 | Inherits filter context; can't drift out of sync |
| Custom tooltip page | Trend charts | Shows a 24h sparkline on hover without a click |
| Bookmarks | Page 1 window toggle | Three views, one canvas |
| Field parameters | Page 2 | One chart, four questions |
| Dynamic titles | All pages | Exported screenshots stay self-describing |
| Conditional formatting | KPI cards, tables | Encodes status without extra visuals |
| What-if parameter | Page 2 | Cost is an *assumption*; a slider makes that honest |
| Decomposition tree | Page 3 | Genuinely hierarchical, exploratory question |
| Key influencers | Page 3 | Surfaces drivers that would take many manual charts |
| Analytics pane trend line | Page 3 scatter | Load/temp response is the point of the visual |

### Features deliberately **not** used

- **Row-level security** — there's no multi-tenant audience here. Adding RLS to a public dataset would be theatre.
- **Forecasting visual** — this report *grades* forecasts. Layering Power BI's own naive forecast on top would be confusing and analytically weak.
- **Maps** — BA footprints are irregular polygons that Power BI can't render accurately from a code. A bad map is worse than no map.
- **AI narrative visual** — we generate a better, deterministic narrative in SQL.

Being able to explain why you *didn't* use a feature is worth as much as using it.
