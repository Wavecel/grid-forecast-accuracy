"""
Ingest: pull from the APIs and land immutable raw Parquet, partitioned by month.

THE DESIGN RULE THAT MATTERS: RAW IS IMMUTABLE AND SEPARATE FROM CURATED
------------------------------------------------------------------------
We never transform on the way in. Raw Parquet is as close to the API response as
typing allows. Everything opinionated happens later, in SQL, against raw.

Why that separation is worth defending in an interview:
  * If a transform has a bug, you fix the SQL and replay -- you do NOT have to
    re-download 25 months from a rate-limited API.
  * If EIA revises a historical hour (they do), you can see the revision because
    raw is versioned in git.
  * The API is the one thing you cannot get back. Treat it as precious.

WHY PARQUET AND NOT CSV FOR RAW
-------------------------------
Columnar + compressed: 25 months x 6 BAs x 4 series is ~1.1M rows, which is
~120 MB as CSV but ~8 MB as Parquet. That is the difference between a repo
GitHub is happy with and one it is not. Curated output is CSV, because that is
what Power BI's Web connector reads most reliably.

MODES
-----
  python -m src.ingest --probe       Dump EIA metadata (schema-drift detection)
  python -m src.ingest --backfill    Full 25-month history (run once, ~5 min)
  python -m src.ingest               Incremental: last 10 days (the hourly job)
  python -m src.ingest --demo        Synthetic fixture, no API key needed,
                                     for building/testing the downstream stack
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, eia_client, weather_client
from .utils import ApiError, append_run_log, get_logger, utcnow, write_freshness

log = get_logger("ingest")

RAW_REGION_DIR = config.RAW_DIR / "region_data"
RAW_FUEL_DIR = config.RAW_DIR / "fuel_mix"
RAW_WEATHER_DIR = config.RAW_DIR / "weather_city"
META_DIR = config.PROJECT_ROOT / "docs" / "api_metadata"

for _d in (RAW_REGION_DIR, RAW_FUEL_DIR, RAW_WEATHER_DIR, META_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Month-partitioned upsert
# ---------------------------------------------------------------------------
def _upsert_monthly(df: pd.DataFrame, out_dir: Path, key_cols: list[str]) -> int:
    """Write rows into month files, replacing any row with a matching key.

    WHY UPSERT RATHER THAN APPEND: the hourly job deliberately re-requests the
    last 10 days, because EIA back-fills and revises recent hours (a demand
    value published at T+1h is provisional; the settled value can differ). A
    blind append would create duplicate hours and double-count demand -- the
    classic incremental-load bug. Upsert on (period, ba, series) means the
    newest published value always wins, and re-running the job is idempotent.

    Idempotence is the property you want to be able to name out loud: running
    this script twice in a row produces exactly the same files as running it
    once. That is what makes an unattended pipeline safe to retry.
    """
    if df.empty:
        return 0

    df = df.copy()
    df["period_month"] = df["period_utc"].dt.strftime("%Y-%m")
    written = 0

    for month, chunk in df.groupby("period_month", sort=True):
        path = out_dir / f"{out_dir.name}_{month}.parquet"
        chunk = chunk.drop(columns=["period_month"])

        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, chunk], ignore_index=True)
            # keep='last' => the freshly fetched row supersedes the stored one
            combined = combined.drop_duplicates(subset=key_cols, keep="last")
        else:
            combined = chunk.drop_duplicates(subset=key_cols, keep="last")

        combined = combined.sort_values(key_cols).reset_index(drop=True)
        combined.to_parquet(path, index=False, compression="snappy")
        written += len(chunk)
        log.info("  %s -> %d new/updated rows (file now %d rows)", path.name, len(chunk), len(combined))

    return written


# ---------------------------------------------------------------------------
# Metadata probe
# ---------------------------------------------------------------------------
def run_probe() -> None:
    """Commit what the API says about itself, so drift shows up as a git diff."""
    started = utcnow()
    out: dict = {"probed_utc": started.isoformat()}

    for route in (config.EIA_REGION_DATA_ROUTE, config.EIA_FUEL_TYPE_ROUTE):
        info = eia_client.probe_route(route)
        out[route] = info
        log.info("route %s | facets=%s | range %s .. %s",
                 route, info["facets"], info["start_period"], info["end_period"])

    respondents = eia_client.probe_available_respondents()
    codes = {r.get("id") for r in respondents}
    out["respondent_count"] = len(codes)
    out["our_bas_present"] = {c: (c in codes) for c in config.BA_CODES}

    missing = [c for c, ok in out["our_bas_present"].items() if not ok]
    if missing:
        log.error("BA codes NOT found in EIA respondent list: %s", missing)
    else:
        log.info("all %d configured BA codes confirmed present in EIA", len(config.BA_CODES))

    path = META_DIR / "eia_api_metadata.json"
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s", path)
    append_run_log("probe", "SUCCESS" if not missing else "WARN", started,
                   rows_written=len(codes), message=f"missing={missing}")


# ---------------------------------------------------------------------------
# Real ingest
# ---------------------------------------------------------------------------
def run_ingest(backfill: bool, skip_weather: bool = False, skip_eia: bool = False) -> None:
    started = utcnow()
    rows_total = 0
    try:
        if backfill:
            start, end = eia_client.default_window(config.BACKFILL_MONTHS)
            log.info("BACKFILL mode: %s .. %s (%d months)", start.date(), end.date(), config.BACKFILL_MONTHS)
        else:
            end = utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            start = end - timedelta(days=10)
            log.info("INCREMENTAL mode: %s .. %s (10-day revision window)", start.date(), end.date())

        # --- EIA region data: D, DF, NG, TI -------------------------------
        if not skip_eia:
            region = eia_client.fetch_region_data(start, end)
            if not region.empty:
                # Stamp real data explicitly, so a demo fixture row is always
                # distinguishable from a real one even after an upsert merge.
                region["is_demo_data"] = 0
                rows_total += _upsert_monthly(
                    region, RAW_REGION_DIR,
                    key_cols=["period_utc", "ba_code", "series_type"],
                )
                log.info("region_data: %d rows landed", len(region))

            # --- EIA fuel mix (renewable share KPI) -----------------------
            fuel = eia_client.fetch_fuel_mix(start, end)
            if not fuel.empty:
                rows_total += _upsert_monthly(
                    fuel, RAW_FUEL_DIR,
                    key_cols=["period_utc", "ba_code", "fuel_code"],
                )
                log.info("fuel_mix: %d rows landed", len(fuel))

        # --- Weather ------------------------------------------------------
        if not skip_weather:
            city = weather_client.fetch_all_cities(start, end, include_forecast=True)
            if not city.empty:
                rows_total += _upsert_monthly(
                    city, RAW_WEATHER_DIR,
                    key_cols=["period_utc", "ba_code", "city", "weather_source"],
                )
                log.info("weather_city: %d rows landed", len(city))

        _stamp_freshness()
        append_run_log("ingest_backfill" if backfill else "ingest_hourly",
                       "SUCCESS", started, rows_written=rows_total)
        log.info("ingest complete: %d rows", rows_total)

    except (ApiError, Exception) as exc:  # noqa: BLE001 - we want the ledger entry
        append_run_log("ingest_backfill" if backfill else "ingest_hourly",
                       "FAILED", started, rows_written=rows_total,
                       message=f"{exc.__class__.__name__}: {exc}")
        log.exception("ingest FAILED")
        raise


def _stamp_freshness() -> None:
    """Record the newest ACTUAL demand hour we hold.

    Note it is the newest *demand* hour that matters, not the newest row: the
    weather forecast reaches into the future, so 'max period in the data' would
    flatter us by ~72 hours. Measuring freshness against the thing the KPI
    depends on is the honest way to do it.
    """
    info: dict = {}
    files = sorted(RAW_REGION_DIR.glob("*.parquet"))
    if files:
        latest = pd.read_parquet(files[-1])
        demand = latest[(latest["series_type"] == "D") & latest["value"].notna()]
        if not demand.empty:
            info["latest_demand_hour_utc"] = demand["period_utc"].max().isoformat()
            info["hours_behind_now"] = round(
                (utcnow() - demand["period_utc"].max().to_pydatetime()).total_seconds() / 3600, 1
            )
        fc = latest[(latest["series_type"] == "DF") & latest["value"].notna()]
        if not fc.empty:
            info["latest_forecast_hour_utc"] = fc["period_utc"].max().isoformat()

    wfiles = sorted(RAW_WEATHER_DIR.glob("*.parquet"))
    if wfiles:
        w = pd.read_parquet(wfiles[-1])
        info["latest_weather_hour_utc"] = w["period_utc"].max().isoformat()

    info["raw_region_files"] = len(files)
    info["ba_codes"] = config.BA_CODES
    write_freshness(info)
    log.info("freshness: %s", json.dumps(info, default=str))


# ---------------------------------------------------------------------------
# Demo fixture (no API key required)
# ---------------------------------------------------------------------------
def run_demo(months: int = 25) -> None:
    """Generate a synthetic-but-structurally-real fixture.

    WHAT THIS IS FOR, AND WHAT IT IS NOT FOR
    ----------------------------------------
    IT IS FOR: building and validating everything downstream -- the SQL, the
    star schema, the DAX, the website -- today, without waiting on an API key.
    Being able to develop against a fixture is normal, professional practice.

    IT IS NOT FOR: your portfolio. Every file it writes is stamped
    is_demo_data=1 and the website renders a loud banner. Before you publish,
    run --backfill with a real key and the demo rows are overwritten.

    The generator uses a real load model (daily + weekly + annual seasonality,
    temperature response, and a deliberately BIASED forecast that under-predicts
    on hot days) so that the analytics have something true to find. Real weather
    is pulled from Open-Meteo -- only demand is synthetic.
    """
    started = utcnow()
    import numpy as np

    rng = np.random.default_rng(42)  # seeded: the fixture is reproducible
    end = utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=int(months * 30.44))

    log.info("DEMO mode: real weather + synthetic demand, %s .. %s", start.date(), end.date())

    city = weather_client.fetch_all_cities(start, end, include_forecast=True)
    _upsert_monthly(city, RAW_WEATHER_DIR,
                    key_cols=["period_utc", "ba_code", "city", "weather_source"])
    wx = weather_client.blend_to_ba(city)

    # Rough real-world peak load (MW) per BA, so magnitudes are plausible.
    scale = {"PJM": 150000, "MISO": 120000, "ERCO": 85000, "CISO": 50000,
             "ISNE": 26000, "NYIS": 32000}

    frames = []
    for ba_code, g in wx.groupby("ba_code"):
        g = g.sort_values("period_utc").reset_index(drop=True)
        peak = scale.get(ba_code, 40000)
        local_hour = g["period_utc"].dt.tz_convert(config.BA_BY_CODE[ba_code].timezone)
        hod = local_hour.dt.hour
        dow = local_hour.dt.dayofweek
        doy = local_hour.dt.dayofyear

        base = 0.62 * peak
        daily = 0.13 * peak * np.sin((hod - 5) / 24 * 2 * np.pi) + \
                0.07 * peak * np.sin((hod - 9) / 12 * 2 * np.pi)
        weekend = np.where(dow >= 5, -0.06 * peak, 0.0)
        annual = 0.04 * peak * np.cos((doy - 200) / 365 * 2 * np.pi)
        cdh = g["cooling_degree_hours"].fillna(0).to_numpy()
        hdh = g["heating_degree_hours"].fillna(0).to_numpy()
        # Non-linear cooling response: AC saturation makes the curve convex.
        cooling = 0.010 * peak * cdh + 0.00035 * peak * cdh ** 2
        heating = 0.0045 * peak * hdh
        noise = rng.normal(0, 0.012 * peak, len(g))

        actual = base + daily + weekend + annual + cooling + heating + noise

        # The forecast: good, but with the two biases real forecasts exhibit --
        # it under-predicts extreme heat and lags sharp weather transitions.
        heat_bias = -0.030 * peak * (cdh > 15).astype(float)
        temp_jump = np.abs(np.diff(g["temp_f"].ffill().to_numpy(), prepend=np.nan))
        transition_bias = -0.012 * peak * np.nan_to_num(temp_jump > 6).astype(float)
        fc_noise = rng.normal(0, 0.016 * peak, len(g))
        forecast = actual + heat_bias + transition_bias + fc_noise

        ng = actual * rng.normal(1.0, 0.02, len(g))
        ti = actual * rng.normal(0.0, 0.03, len(g))

        for series_type, series_name, vals in [
            ("D", "Demand", actual),
            ("DF", "Day-ahead demand forecast", forecast),
            ("NG", "Net generation", ng),
            ("TI", "Total interchange", ti),
        ]:
            frames.append(pd.DataFrame({
                "period_utc": g["period_utc"],
                "ba_code": ba_code,
                "ba_name_eia": config.BA_BY_CODE[ba_code].name,
                "series_type": series_type,
                "series_name": series_name,
                "value": np.round(vals, 0),
                "value_units": "megawatthours",
                "is_demo_data": 1,
            }))

    region = pd.concat(frames, ignore_index=True)
    # Demand and forecast do not exist for future hours -- only weather does.
    # Blank them so the fixture has the same shape as reality.
    future = region["period_utc"] > utcnow().replace(minute=0, second=0, microsecond=0)
    region.loc[future & region["series_type"].isin(["D", "NG", "TI"]), "value"] = pd.NA

    n = _upsert_monthly(region, RAW_REGION_DIR,
                        key_cols=["period_utc", "ba_code", "series_type"])
    _stamp_freshness()
    append_run_log("ingest_demo", "SUCCESS", started, rows_written=n,
                   message="SYNTHETIC demand - not for publication")
    log.warning("DEMO fixture written (%d rows). Values are SYNTHETIC. "
                "Run --backfill with a real EIA_API_KEY before publishing.", n)


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ingest grid + weather data into raw Parquet")
    p.add_argument("--backfill", action="store_true", help="full history pull (run once)")
    p.add_argument("--probe", action="store_true", help="dump EIA API metadata only")
    p.add_argument("--demo", action="store_true", help="synthetic fixture, no API key needed")
    p.add_argument("--months", type=int, default=config.BACKFILL_MONTHS,
                   help="history length in months (backfill/demo)")
    p.add_argument("--skip-weather", action="store_true")
    p.add_argument("--skip-eia", action="store_true")
    args = p.parse_args(argv)

    # Load .env if present so local runs pick up EIA_API_KEY automatically.
    env_path = config.PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                import os
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        # config was imported before .env was read, so refresh the cached value
        import os
        config.EIA_API_KEY = os.environ.get("EIA_API_KEY", config.EIA_API_KEY)
        eia_client.config.EIA_API_KEY = config.EIA_API_KEY

    if args.probe:
        run_probe()
    elif args.demo:
        run_demo(months=args.months)
    else:
        config.BACKFILL_MONTHS = args.months
        run_ingest(backfill=args.backfill, skip_weather=args.skip_weather, skip_eia=args.skip_eia)
    return 0


if __name__ == "__main__":
    sys.exit(main())
