"""
Build the compact JSON payload that the public website reads.

WHY A SEPARATE, SMALLER DATASET FOR THE WEB
-------------------------------------------
The Power BI model and the website have genuinely different constraints, and
pretending otherwise produces a bad version of both:

    POWER BI  wants the full hourly grain (~110,000 rows). It has a compression
              engine (VertiPaq), a query language, and a desktop-class client.
              Give it everything and let the user slice.

    BROWSER   has none of that. Every byte crosses the network before anything
              renders. Shipping 110,000 rows as JSON would be roughly 25 MB and
              several seconds of parse time on a phone -- for a page that only
              ever displays daily aggregates and the last two weeks of hours.

So we ship purpose-built extracts: ~400 KB total, everything pre-aggregated to
exactly the shape each chart consumes. The site loads instantly and works on a
phone on mobile data, which is how a recruiter will actually open it.

THE FORMAT CHOICE: COLUMNAR ARRAYS, NOT ARRAY-OF-OBJECTS
--------------------------------------------------------
    Array-of-objects: [{"date":"2026-08-01","mape":0.0231}, ...]
    Columnar:         {"date":["2026-08-01",...], "mape":[0.0231,...]}

The second repeats each key ONCE instead of once per row. For 4,500 daily rows
with 20 fields that is the difference between ~1.4 MB and ~380 KB -- the same
insight that makes Parquet fast, applied to JSON. Charting libraries want
columns anyway, so no client-side reshaping is needed.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from . import config
from .utils import append_run_log, get_logger, iso, read_freshness, utcnow

log = get_logger("build_web")

WEB_DIR = config.PROJECT_ROOT / "web" / "data"
WEB_DIR.mkdir(parents=True, exist_ok=True)

# Only the most recent slice is needed for the public site.
DAILY_DAYS = 400          # covers a full year plus context for YoY commentary
HOURLY_DAYS = 14          # the detail chart window
ALERT_LOOKBACK_DAYS = 3   # "what needs attention" horizon


def _clean(v: Any) -> Any:
    """JSON has no NaN. Convert every missing value to null exactly once, here.

    Skipping this step is a classic own-goal: json.dumps happily writes the
    literal token NaN, which is valid Python and invalid JSON, and the browser
    fails with an unhelpful parse error at byte 40,000.
    """
    if v is None:
        return None
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else round(v, 6)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if hasattr(v, "item"):          # numpy scalar
        try:
            v = v.item()
        except Exception:           # noqa: BLE001
            return str(v)
        return _clean(v)
    if isinstance(v, float):
        return _clean(float(v))
    return v


def _columnar(df: pd.DataFrame) -> dict[str, list]:
    """DataFrame -> {column_name: [values]} with NaN converted to null."""
    out: dict[str, list] = {}
    for col in df.columns:
        out[col] = [_clean(v) for v in df[col].tolist()]
    return out


def _write(name: str, payload: Any) -> None:
    path = WEB_DIR / name
    # separators without spaces trims another ~8% off the wire size for free.
    path.write_text(json.dumps(payload, separators=(",", ":"), default=str), encoding="utf-8")
    log.info("wrote %-22s %8.1f KB", name, path.stat().st_size / 1024)


def build(db_path: Path | None = None) -> None:
    started = utcnow()
    db_path = db_path or (config.DATA_DIR / "warehouse.duckdb")
    if not db_path.exists():
        raise RuntimeError("warehouse.duckdb not found -- run `python -m src.transform` first")

    con = duckdb.connect(str(db_path), read_only=True)
    con.execute("LOAD icu; SET TimeZone='UTC';")

    # -------------------------------------------------------------------
    # 1. META -- provenance and freshness
    # -------------------------------------------------------------------
    # Rendered as a banner on the site. Showing how stale the data is, and
    # whether it is the synthetic fixture, costs nothing and buys credibility.
    # A dashboard that hides its own staleness is worse than no dashboard.
    meta_row = con.execute("""
        SELECT
            MAX(period_utc) FILTER (WHERE row_kind = 'actual')   AS latest_actual_utc,
            MAX(period_utc)                                      AS latest_any_utc,
            MIN(date_key)                                        AS first_date,
            MAX(date_key) FILTER (WHERE row_kind = 'actual')     AS last_actual_date,
            COUNT(*)                                             AS fact_rows,
            MAX(is_demo_data)                                    AS is_demo
        FROM fact_load_hour
    """).df().iloc[0]

    dq = con.execute("SELECT check_name, severity, result, observed, expected, description FROM dq_checks").df()

    meta = {
        "generated_utc": iso(utcnow()),
        "latest_actual_hour_utc": _clean(meta_row["latest_actual_utc"]),
        "latest_data_hour_utc": _clean(meta_row["latest_any_utc"]),
        "history_start": _clean(meta_row["first_date"]),
        "history_end": _clean(meta_row["last_actual_date"]),
        "fact_rows": int(meta_row["fact_rows"]),
        "is_demo_data": bool(meta_row["is_demo"]),
        "hours_behind": _clean(
            (utcnow() - pd.Timestamp(meta_row["latest_actual_utc"]).to_pydatetime()).total_seconds() / 3600
        ) if pd.notna(meta_row["latest_actual_utc"]) else None,
        "dq_pass": int((dq["result"] == "PASS").sum()),
        "dq_total": int(len(dq)),
        "dq_checks": _columnar(dq),
        "pipeline": read_freshness(),
    }
    _write("meta.json", meta)

    # -------------------------------------------------------------------
    # 2. BALANCING AUTHORITIES -- the dimension, plus headline KPIs
    # -------------------------------------------------------------------
    ba = con.execute(f"""
        WITH scored AS (
            SELECT ba_code,
                   AVG(ape)                                          AS mape_all,
                   AVG(signed_pe)                                    AS bias_all,
                   AVG(ape) FILTER (WHERE date_key >= CURRENT_DATE - 30)  AS mape_30d,
                   AVG(ape) FILTER (WHERE date_key >= CURRENT_DATE - 7)   AS mape_7d,
                   AVG(signed_pe) FILTER (WHERE date_key >= CURRENT_DATE - 30) AS bias_30d,
                   AVG(ape) FILTER (WHERE is_daily_peak_hour = 1)     AS mape_peak_hour,
                   MAX(demand_mw)                                    AS max_demand_mw,
                   AVG(demand_mw)                                    AS avg_demand_mw,
                   SUM(is_material_miss)                             AS material_misses,
                   COUNT(*) FILTER (WHERE row_kind = 'actual')       AS gradeable_hours
            FROM fact_load_hour
            WHERE row_kind = 'actual' AND ape IS NOT NULL
            GROUP BY ba_code
        )
        SELECT d.ba_code, d.ba_name, d.ba_label, d.market, d.region, d.timezone,
               d.peak_season, d.size_band, d.weather_cities, d.weather_city_count,
               s.mape_all, s.bias_all, s.mape_30d, s.mape_7d, s.bias_30d,
               s.mape_peak_hour, s.max_demand_mw, s.avg_demand_mw,
               s.material_misses, s.gradeable_hours
        FROM dim_ba d
        LEFT JOIN scored s USING (ba_code)
        ORDER BY s.mape_30d DESC NULLS LAST
    """).df()
    _write("balancing_authorities.json", _columnar(ba))

    # -------------------------------------------------------------------
    # 3. DAILY SERIES -- the main time-series chart + the brief
    # -------------------------------------------------------------------
    daily = con.execute(f"""
        SELECT ba_code, date_key, day_type, holiday_name,
               mape, bias_pct, peak_hour_ape, peak_hour_error_mw,
               mape_baseline_same_daytype, mape_vs_baseline_pct, baseline_sample_days,
               avg_demand_mw, peak_demand_mw, peak_hour_local, peak_vs_baseline_pct,
               total_abs_error_mwh, material_miss_hours, worst_period,
               worst_hour_local, worst_hour_error_mw, max_ape_zscore,
               avg_temp_f, max_temp_f, cooling_degree_hours, heating_degree_hours,
               max_abs_ramp_mw, peak_to_trough_mw,
               hours_with_actual, status_band, daily_narrative
        FROM agg_daily_brief
        WHERE date_key >= CURRENT_DATE - {DAILY_DAYS}
          AND hours_with_actual > 0
        ORDER BY ba_code, date_key
    """).df()
    _write("daily.json", _columnar(daily))

    # -------------------------------------------------------------------
    # 4. RECENT HOURLY -- actual vs forecast detail chart
    # -------------------------------------------------------------------
    hourly = con.execute(f"""
        SELECT ba_code, period_utc, local_datetime, local_hour, date_key, row_kind,
               demand_mw, forecast_mw, forecast_error_mw, ape, signed_pe,
               temp_f, cooling_degree_hours, ramp_mw,
               is_daily_peak_hour, is_material_miss, ape_zscore
        FROM fact_load_hour
        WHERE date_key >= CURRENT_DATE - {HOURLY_DAYS}
        ORDER BY ba_code, period_utc
    """).df()
    _write("hourly_recent.json", _columnar(hourly))

    # -------------------------------------------------------------------
    # 5. PROFILES -- the "why" charts
    # -------------------------------------------------------------------
    # 5a. Hour-of-day error profile: WHEN does the forecast fail?
    hour_profile = con.execute("""
        SELECT f.ba_code, f.local_hour, h.day_period, h.day_period_sort,
               AVG(f.ape)            AS mape,
               AVG(f.signed_pe)      AS bias,
               AVG(f.demand_mw)      AS avg_demand_mw,
               AVG(f.abs_error_mw)   AS avg_abs_error_mw,
               COUNT(*)              AS hours
        FROM fact_load_hour f
        JOIN dim_hour h ON h.hour_of_day = f.local_hour
        WHERE f.row_kind = 'actual' AND f.ape IS NOT NULL
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2
    """).df()
    _write("profile_hour.json", _columnar(hour_profile))

    # 5b. Temperature-response profile: WHY does it fail?
    # This is the analytical centrepiece of the whole project. Bucketing by
    # cooling degree hours and reporting SIGNED bias per bucket is what turns
    # "the forecast is sometimes wrong" into "the forecast is systematically
    # low when it is hot" -- a claim a forecasting team can actually act on.
    temp_profile = con.execute("""
        SELECT ba_code,
               CASE
                   WHEN cooling_degree_hours >= 20 THEN 20
                   ELSE FLOOR(cooling_degree_hours / 2.5) * 2.5
               END                          AS cdh_bucket,
               AVG(ape)                     AS mape,
               AVG(signed_pe)               AS bias,
               AVG(demand_mw)               AS avg_demand_mw,
               AVG(forecast_error_mw)       AS avg_error_mw,
               COUNT(*)                     AS hours
        FROM fact_load_hour
        WHERE row_kind = 'actual' AND ape IS NOT NULL AND cooling_degree_hours IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) >= 20
        ORDER BY 1, 2
    """).df()
    _write("profile_temperature.json", _columnar(temp_profile))

    # 5c. Demand vs temperature scatter -- the load/temperature response curve.
    # Down-sampled to keep the payload small; the shape is what matters, and
    # every 6th hour preserves it perfectly while cutting the size by 83%.
    scatter = con.execute("""
        SELECT ba_code, temp_f, demand_mw, forecast_error_mw, local_hour, day_type
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ba_code ORDER BY period_utc) AS rn
            FROM fact_load_hour
            WHERE row_kind = 'actual' AND temp_f IS NOT NULL AND demand_mw IS NOT NULL
        )
        WHERE rn % 6 = 0
        ORDER BY ba_code, temp_f
    """).df()
    _write("scatter_temp_demand.json", _columnar(scatter))

    # -------------------------------------------------------------------
    # 6. ALERTS -- "what requires attention today?"
    # -------------------------------------------------------------------
    # Ranked, not just listed. An unranked alert list is a to-do list nobody
    # works through; a ranked one tells the user where to start. Severity is
    # driven by the z-score against each BA's own 30-day error distribution, so
    # a 3% miss is an alert for a normally-accurate BA and routine for a
    # volatile one.
    alerts = con.execute(f"""
        SELECT f.ba_code, b.ba_name, f.period_utc, f.local_datetime, f.local_hour,
               f.date_key, f.demand_mw, f.forecast_mw, f.forecast_error_mw,
               f.ape, f.signed_pe, f.ape_zscore, f.temp_f, f.cooling_degree_hours,
               f.is_daily_peak_hour, f.ramp_mw,
               CASE
                   WHEN f.ape_zscore >= 4 THEN 'Critical'
                   WHEN f.ape_zscore >= 3 THEN 'High'
                   WHEN f.ape_zscore >= 2 THEN 'Elevated'
                   ELSE 'Normal'
               END AS severity,
               CASE
                   WHEN f.signed_pe > 0 THEN 'Under-forecast (system short)'
                   ELSE 'Over-forecast (system long)'
               END AS direction
        FROM fact_load_hour f
        JOIN dim_ba b USING (ba_code)
        WHERE f.row_kind = 'actual'
          AND f.date_key >= CURRENT_DATE - {ALERT_LOOKBACK_DAYS}
          AND f.ape_zscore >= 2
        ORDER BY f.ape_zscore DESC
        LIMIT 60
    """).df()
    _write("alerts.json", _columnar(alerts))

    # -------------------------------------------------------------------
    # 7. FORWARD OUTLOOK -- the peak-risk early warning
    # -------------------------------------------------------------------
    # This is the payoff of the FULL OUTER JOIN in 04_fact_load_hour.sql: rows
    # that have weather but no actual demand yet. We compare the forward
    # temperature against the recent observed maximum to flag heat that the
    # system has not been tested against lately -- turning a report about the
    # past into a warning about tomorrow.
    outlook = con.execute("""
        WITH recent_max AS (
            SELECT ba_code,
                   MAX(temp_f)     AS recent_max_temp,
                   MAX(demand_mw)  AS recent_max_demand
            FROM fact_load_hour
            WHERE row_kind = 'actual' AND date_key >= CURRENT_DATE - 30
            GROUP BY ba_code
        )
        SELECT f.ba_code, b.ba_name, f.period_utc, f.local_datetime, f.date_key,
               f.local_hour, f.row_kind, f.temp_f, f.cooling_degree_hours,
               f.forecast_mw, r.recent_max_temp, r.recent_max_demand,
               ROUND(f.temp_f - r.recent_max_temp, 1) AS temp_vs_recent_max
        FROM fact_load_hour f
        JOIN dim_ba b USING (ba_code)
        LEFT JOIN recent_max r USING (ba_code)
        WHERE f.row_kind IN ('forecast_only', 'weather_only')
          AND f.temp_f IS NOT NULL
        ORDER BY f.ba_code, f.period_utc
    """).df()
    _write("outlook.json", _columnar(outlook))

    # -------------------------------------------------------------------
    # 8. INDEX -- one file listing the others, with sizes
    # -------------------------------------------------------------------
    # Lets the site fetch in parallel and show a real loading state, and lets
    # anyone reusing the data discover it without reading the source.
    index = {
        "generated_utc": iso(utcnow()),
        "files": [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in sorted(WEB_DIR.glob("*.json")) if p.name != "index.json"
        ],
    }
    total_kb = sum(f["bytes"] for f in index["files"]) / 1024
    index["total_kb"] = round(total_kb, 1)
    _write("index.json", index)

    con.close()
    log.info("web payload complete: %.1f KB across %d files", total_kb, len(index["files"]))
    append_run_log("build_web_data", "SUCCESS", started, rows_written=len(daily))


def main() -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
