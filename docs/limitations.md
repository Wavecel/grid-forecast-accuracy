# Limitations

Stated plainly. Volunteering these before being asked is a strength; being caught not knowing them is not.

---

## Analytical

### The weather proxy is coarse
Each BA is represented by a population-weighted blend of 4–6 metros, not true load-weighted weather.

- **Worst case: MISO.** Minneapolis and New Orleans in one blend. Its footprint spans ~1,500 km north to south with genuinely different climates; a single blended temperature smooths away real variation.
- **Best case: ERCOT.** Four large Texas metros in a relatively uniform climate.
- **Consequence:** some apparent forecast bias may be *proxy* error rather than *model* error. The direction of the finding is robust; the magnitude should be treated as indicative.
- **Fix, if this were production:** actual load-weighted weather from the BA's own metering zones. Not free.

### Correlation, not causation
The bias/cooling-degree-hour relationship is an hourly-grain association. It is consistent with an under-modelled air-conditioning response, but it does not prove one. Alternatives worth ruling out:

- Operators may under-forecast **deliberately** in some markets for reserve-procurement reasons.
- Hot days correlate with other things (day of week clustering in summer, holiday timing).
- The proxy issue above.

The dashboard wording is hedged throughout ("consistent with", "coincided with"). Keep it that way.

### No cost quantification
Real imbalance settlement prices — the real-time vs day-ahead spread by interval and market — are **not free data**. Every dollar figure in this project is an explicit user-set **scenario** via a what-if parameter, labelled as such, and never presented as a measured result. The resume bullets quote no dollar impact.

### MAPE has known pathologies
It's asymmetric (penalises over-forecasts more than under-forecasts for the same absolute error) and undefined at zero demand. Both are acceptable here because BA-level demand is never near zero and the industry convention is MAPE — but you should know this if challenged. `MAPE (Load Weighted)` is provided alongside for comparison.

### Six BAs is not the whole US
There are ~66 balancing authorities. These six cover a large share of US load and span distinct regimes, but conclusions don't automatically generalise to small BAs, which are structurally harder to forecast (less load diversity means individual behaviour is averaged away less).

---

## Data

### EIA revises history
Recent hours are provisional and can change after first publication. The pipeline re-pulls a rolling 10-day window and upserts, so figures for the last few days can move slightly. This is correct behaviour, not a bug — but a screenshot taken today may not exactly match the same date next week.

### Forecast coverage is incomplete
EIA does not publish `DF` for every BA in every hour. MAPE computed over a biased subset of hours is not strictly comparable across BAs. The `forecast_coverage` check quantifies this and warns above 5%.

### DST days have 23 or 25 hours
Handled correctly (ICU `AT TIME ZONE`), and the `ba_day_hour_coverage` check is `WARN` rather than `FAIL` specifically to accommodate it. Worth knowing that daily aggregates on those two days per year cover a different number of hours.

### Open-Meteo forecast ≠ operator forecast
The forward weather used on the Outlook page is Open-Meteo's model, not the weather forecast the balancing authority actually used when producing its day-ahead load forecast. So the Outlook page is an *independent* view of forward risk, not a reconstruction of the operator's inputs.

### ERA5 archive vs live forecast are different products
Hours sourced from the forecast endpoint (roughly the last 5 days) are not reanalysis-quality. The `weather_source` column labels every hour's provenance so this is visible rather than hidden.

---

## Technical

### Git as a data store has a ceiling
This works at ~110k curated rows refreshed hourly. It would not work at 13M rows or 5-minute granularity — repo size and commit churn would become unmanageable. The honest breakpoint is roughly 1 GB of repo. Beyond that: object storage (Cloudflare R2 / S3), still near-free.

### Power BI Free cannot share
Scheduled refresh in *My Workspace* works on the free licence (8×/day). Sharing a report link requires Pro at $14/user/month. This is a real constraint on the "$0" claim and it's stated in the README rather than buried.

### Publish to web is tenant-dependent
May be disabled by your organisation's Power BI admin. Also genuinely public — never appropriate for real company data.

### GitHub Actions cron is best-effort
Scheduled workflows can be delayed or dropped under load. Running at `:12` mitigates but does not eliminate this. A missed run self-heals on the next one because of the 10-day re-pull window.

### No unit tests on the transform
There are 12 integration-level data-quality assertions that gate the build, which is more than most projects have — but there are no true unit tests seeding a known-input fixture and asserting exact expected output. This is the most significant gap in the codebase and is named as such rather than glossed over.

### The demo fixture
`--demo` generates synthetic demand (real weather) so the stack can be developed without an API key. Every row is stamped `is_demo_data=1`, a DQ check flags it, and the website shows a banner. **It must never appear in a published portfolio version.** The `no_demo_data` check exists precisely to prevent that.

---

## Scope decisions (not defects)

Things deliberately left out, with reasons:

| Not included | Why |
|---|---|
| Building a better load forecast | The project *grades* forecasts. Building a competing one is a different, much larger project and would muddy the analysis |
| Real-time (5-min) data | EIA publishes hourly at BA level; sub-hourly isn't available free |
| Locational marginal prices | Not free at the granularity needed |
| Net-load analysis (demand − wind − solar) | The natural next step; noted in the interview prep as the follow-up |
| Row-level security | No multi-tenant audience on a public dataset; adding it would be theatre |
| Maps | BA footprints are irregular polygons Power BI cannot render accurately from a code. A wrong map is worse than no map |
