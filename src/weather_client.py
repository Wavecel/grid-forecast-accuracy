"""
Open-Meteo client -- the weather driver behind electricity demand.

WHY WEATHER IS THE CENTRE OF THIS PROJECT
-----------------------------------------
Roughly 70-90% of the short-run variance in electricity demand is temperature.
A load forecast is, at heart, a weather forecast plus a behavioural model. So if
we want to explain *why* a forecast missed, we cannot do it without weather --
"the forecast was wrong" is not an insight; "the forecast under-predicted by
4.1% on the first three days of the heat wave, when cooling load rose 38 MW per
degree F above 85F" is.

TWO ENDPOINTS, ONE SEAM (this is the tricky bit)
------------------------------------------------
  archive-api.open-meteo.com/v1/archive   ERA5 reanalysis. Excellent quality,
                                          reaches back decades, but LAGS real
                                          time by roughly 5 days.
  api.open-meteo.com/v1/forecast          Live model. Covers `past_days` (up to
                                          ~92) and forward days, but is not the
                                          reanalysis product.

Neither endpoint alone covers "25 months ago -> tomorrow". You must stitch:
      [ ---------------- archive ---------------- ][ overlap ][ -- forecast -- ]
                                                   ^ de-duplicate here
Get this wrong and you get a silent 5-day hole in the MOST RECENT data -- the
exact window every daily-brief page depends on. This is the single most common
weather-pipeline bug and it is invisible until someone asks why yesterday is
blank.

Both endpoints are free, require no key and no account. Open-Meteo asks that
non-commercial use stay under roughly 10,000 calls/day; we make about 30 per
hourly run, so we are three orders of magnitude inside the limit.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import config
from .config import BALANCING_AUTHORITIES, BalancingAuthority, WeatherCity
from .utils import get_logger, http_get_json

log = get_logger("weather_client")


# ---------------------------------------------------------------------------
# Single-city pulls
# ---------------------------------------------------------------------------
def _hourly_frame(payload: dict, city: WeatherCity, ba_code: str, source: str) -> pd.DataFrame:
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        log.warning("%s/%s: %s returned no hourly block", ba_code, city.name, source)
        return pd.DataFrame()

    df = pd.DataFrame({
        # timezone=UTC was requested, so these strings are naive-UTC.
        "period_utc": pd.to_datetime(times, utc=True),
        "temp_f": pd.to_numeric(hourly.get("temperature_2m", [None] * len(times)), errors="coerce"),
        "humidity_pct": pd.to_numeric(hourly.get("relative_humidity_2m", [None] * len(times)), errors="coerce"),
        "wind_mph": pd.to_numeric(hourly.get("wind_speed_10m", [None] * len(times)), errors="coerce"),
        "cloud_pct": pd.to_numeric(hourly.get("cloud_cover", [None] * len(times)), errors="coerce"),
    })
    df["ba_code"] = ba_code
    df["city"] = city.name
    df["city_weight"] = city.weight
    df["weather_source"] = source
    return df


def fetch_city_archive(city: WeatherCity, ba_code: str, start: date, end: date) -> pd.DataFrame:
    """Historical (ERA5) hourly weather. One call can span years -- no chunking
    needed, which is why the backfill is fast."""
    payload = http_get_json(
        config.OPEN_METEO_ARCHIVE,
        {
            "latitude": city.lat,
            "longitude": city.lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "hourly": config.WEATHER_HOURLY_VARS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "UTC",
        },
        context=f"archive {city.name}",
    )
    return _hourly_frame(payload, city, ba_code, "archive")


def fetch_city_recent_and_forecast(city: WeatherCity, ba_code: str) -> pd.DataFrame:
    """Recent past (fills the archive lag) plus forward days (peak-risk warning)."""
    payload = http_get_json(
        config.OPEN_METEO_FORECAST,
        {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": config.WEATHER_HOURLY_VARS,
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": "UTC",
            "past_days": config.FORECAST_PAST_DAYS,
            "forecast_days": config.FORECAST_AHEAD_DAYS,
        },
        context=f"forecast {city.name}",
    )
    return _hourly_frame(payload, city, ba_code, "forecast")


# ---------------------------------------------------------------------------
# Stitch + aggregate to BA level
# ---------------------------------------------------------------------------
def fetch_all_cities(start_utc: datetime, end_utc: datetime, include_forecast: bool = True) -> pd.DataFrame:
    """City-grain weather for every BA across the whole window.

    Archive is requested only up to (today - ARCHIVE_LAG_DAYS); everything newer
    comes from the forecast endpoint's past_days window. We request generous
    overlap on purpose and resolve it deterministically in `blend_to_ba`:
    archive wins wherever both exist, because reanalysis is the better product.
    """
    archive_end = (datetime.now(timezone.utc) - timedelta(days=config.ARCHIVE_LAG_DAYS)).date()
    archive_start = start_utc.date()

    frames: list[pd.DataFrame] = []
    for ba in BALANCING_AUTHORITIES:
        for city in ba.cities:
            if archive_start <= archive_end:
                frames.append(fetch_city_archive(city, ba.code, archive_start, archive_end))
            if include_forecast:
                frames.append(fetch_city_recent_and_forecast(city, ba.code))
        log.info("weather pulled for %s (%d cities)", ba.code, len(ba.cities))

    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def blend_to_ba(city_df: pd.DataFrame) -> pd.DataFrame:
    """Collapse city-grain weather into one population-weighted row per BA-hour.

    HOW THE WEIGHTING WORKS
    -----------------------
    weighted_temp = SUM(city_temp * city_weight) / SUM(city_weight_where_present)

    Note the denominator: we divide by the weight of the cities that actually
    returned data, not by the full declared weight. If Fresno's series has a gap,
    a fixed denominator would drag CISO's blended temperature toward zero and
    manufacture a fake cold snap. Renormalising on present weight is the correct
    handling of partial data and is a genuinely good thing to be able to explain.

    DE-DUPLICATION
    --------------
    archive and forecast overlap by design. We sort so 'archive' sorts before
    'forecast' and keep the first row per (ba, city, hour). Deterministic,
    order-independent, and it prefers the higher-quality source.
    """
    if city_df.empty:
        return pd.DataFrame()

    df = city_df.copy()
    # 'archive' < 'forecast' alphabetically, so ascending sort puts archive first.
    df = df.sort_values(["ba_code", "city", "period_utc", "weather_source"])
    df = df.drop_duplicates(subset=["ba_code", "city", "period_utc"], keep="first")

    df["has_temp"] = df["temp_f"].notna()
    df["w"] = df["city_weight"].where(df["has_temp"], 0.0)

    for col in ("temp_f", "humidity_pct", "wind_mph", "cloud_pct"):
        df[f"_wx_{col}"] = df[col] * df["w"]

    grouped = df.groupby(["ba_code", "period_utc"], as_index=False).agg(
        w_sum=("w", "sum"),
        temp_num=("_wx_temp_f", "sum"),
        hum_num=("_wx_humidity_pct", "sum"),
        wind_num=("_wx_wind_mph", "sum"),
        cloud_num=("_wx_cloud_pct", "sum"),
        cities_reporting=("has_temp", "sum"),
        cities_total=("city", "nunique"),
        # If ANY contributing city came from the forecast model, label the hour
        # 'forecast' so the dashboard can be honest about provenance.
        sources=("weather_source", lambda s: "archive" if set(s) == {"archive"} else "mixed/forecast"),
    )

    safe_w = grouped["w_sum"].replace(0.0, pd.NA)
    grouped["temp_f"] = grouped["temp_num"] / safe_w
    grouped["humidity_pct"] = grouped["hum_num"] / safe_w
    grouped["wind_mph"] = grouped["wind_num"] / safe_w
    grouped["cloud_pct"] = grouped["cloud_num"] / safe_w

    # Degree-hours: the utility industry's standard weather-normalisation unit.
    base = config.DEGREE_HOUR_BASE_F
    grouped["cooling_degree_hours"] = (grouped["temp_f"] - base).clip(lower=0)
    grouped["heating_degree_hours"] = (base - grouped["temp_f"]).clip(lower=0)

    # Coverage ratio is a first-class data-quality column, not a debug field.
    grouped["weather_coverage"] = grouped["cities_reporting"] / grouped["cities_total"]

    out = grouped[[
        "ba_code", "period_utc", "temp_f", "humidity_pct", "wind_mph", "cloud_pct",
        "cooling_degree_hours", "heating_degree_hours",
        "cities_reporting", "cities_total", "weather_coverage", "sources",
    ]].rename(columns={"sources": "weather_source"})

    log.info("blended weather: %d BA-hour rows, %d BAs", len(out), out["ba_code"].nunique())
    return out.sort_values(["ba_code", "period_utc"]).reset_index(drop=True)
