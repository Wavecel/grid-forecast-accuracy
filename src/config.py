"""
Central configuration for the Grid Load Forecast Accuracy project.

WHY THIS FILE EXISTS
--------------------
Every assumption that a reviewer might challenge lives here, in one place:
which balancing authorities (BAs) we cover, how we build a weather proxy for
each one, and what temperature we call "neutral". Hard-coding these inside the
ingest or transform scripts would make the project unauditable -- an interviewer
asking "how did you decide Boston represents ISO New England?" would have to
read three files to find the answer. Now it's one.

KEY DOMAIN CONCEPT
------------------
A "balancing authority" is the entity legally responsible for keeping
electricity supply and demand matched in real time inside a defined footprint.
Each BA publishes, every hour:
    D  = actual demand           (what people really used)
    DF = day-ahead demand forecast (what the BA predicted the day before)
Having BOTH is the entire reason this project works. Almost no free dataset
gives you a prediction and its outcome side by side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"          # immutable API responses, partitioned
CURATED_DIR = DATA_DIR / "curated"  # star-schema tables Power BI reads
LOG_DIR = DATA_DIR / "logs"         # run-log + freshness watchdog
SQL_DIR = PROJECT_ROOT / "sql"
WEB_DATA_DIR = PROJECT_ROOT / "web" / "data"

for _d in (RAW_DIR, CURATED_DIR, LOG_DIR, WEB_DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# EIA API v2
# ---------------------------------------------------------------------------
# Register for a free key in ~30 seconds: https://www.eia.gov/opendata/register.php
# The key is read from the environment so it NEVER enters git history. Locally
# you can drop it in a .env file (gitignored); in CI it comes from a GitHub
# Actions repository secret.
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")

EIA_BASE = "https://api.eia.gov/v2"

# Hourly demand / forecast / generation / interchange, one row per BA-hour-type.
EIA_REGION_DATA_ROUTE = "electricity/rto/region-data"
# Hourly net generation split by fuel (used for the renewable-share KPI).
EIA_FUEL_TYPE_ROUTE = "electricity/rto/fuel-type-data"

# EIA caps a single response at 5000 rows. We page with offset until exhausted.
EIA_PAGE_SIZE = 5000

# The four series we pull from region-data. `type` is an EIA facet value.
#   D  Demand                     -- the actual outcome
#   DF Day-ahead demand forecast  -- the prediction we are grading
#   NG Net generation             -- supply actually produced in-footprint
#   TI Total interchange          -- net exports (+) / imports (-)
EIA_SERIES_TYPES = ["D", "DF", "NG", "TI"]

# Fuel types we roll up into "renewable" for the share KPI.
RENEWABLE_FUEL_CODES = ["SUN", "WND", "WAT"]  # solar, wind, hydro

# ---------------------------------------------------------------------------
# Balancing authorities in scope
# ---------------------------------------------------------------------------
# WHY THESE SIX: they are the largest, most data-complete US BAs and they span
# genuinely different load-shape regimes, which is what makes the analysis
# interesting rather than repetitive:
#   ERCO  summer-peaking, extreme cooling load, famous for scarcity events
#   CISO  solar-saturated "duck curve", steep evening ramps
#   ISNE  winter/summer dual peak, small and weather-whippy
#   NYIS  dense urban load, sharp summer peaks
#   PJM   largest US market, mixed climate, very stable forecasting
#   MISO  north-south sprawl, so its weather proxy is deliberately hard
#
# WEATHER PROXY DESIGN: a BA covers a huge area, so no single city represents
# it. We take a population-weighted blend of major load centres. The weights are
# rough metro-population shares and are declared here so they can be criticised
# openly -- that is the honest way to handle a modelling assumption.
@dataclass(frozen=True)
class WeatherCity:
    name: str
    lat: float
    lon: float
    weight: float


@dataclass(frozen=True)
class BalancingAuthority:
    code: str            # EIA `respondent` facet value
    name: str            # human-readable
    market: str          # organised market / region label
    timezone: str        # IANA tz used to derive local hour-of-day
    peak_season: str     # dominant peak season, used in commentary
    cities: list[WeatherCity] = field(default_factory=list)

    @property
    def total_weight(self) -> float:
        return sum(c.weight for c in self.cities)


BALANCING_AUTHORITIES: list[BalancingAuthority] = [
    BalancingAuthority(
        code="PJM",
        name="PJM Interconnection",
        market="PJM",
        timezone="America/New_York",
        peak_season="Summer",
        cities=[
            WeatherCity("Chicago, IL",      41.8781, -87.6298, 0.24),
            WeatherCity("Philadelphia, PA", 39.9526, -75.1652, 0.20),
            WeatherCity("Washington, DC",   38.9072, -77.0369, 0.18),
            WeatherCity("Baltimore, MD",    39.2904, -76.6122, 0.14),
            WeatherCity("Pittsburgh, PA",   40.4406, -79.9959, 0.12),
            WeatherCity("Richmond, VA",     37.5407, -77.4360, 0.12),
        ],
    ),
    BalancingAuthority(
        code="MISO",
        name="Midcontinent ISO",
        market="MISO",
        timezone="America/Chicago",
        peak_season="Summer",
        cities=[
            WeatherCity("Detroit, MI",       42.3314, -83.0458, 0.24),
            WeatherCity("Minneapolis, MN",   44.9778, -93.2650, 0.22),
            WeatherCity("St. Louis, MO",     38.6270, -90.1994, 0.16),
            WeatherCity("Indianapolis, IN",  39.7684, -86.1581, 0.14),
            WeatherCity("New Orleans, LA",   29.9511, -90.0715, 0.12),
            WeatherCity("Des Moines, IA",    41.5868, -93.6250, 0.12),
        ],
    ),
    BalancingAuthority(
        code="ERCO",
        name="ERCOT (Texas)",
        market="ERCOT",
        timezone="America/Chicago",
        peak_season="Summer",
        cities=[
            WeatherCity("Houston, TX",      29.7604, -95.3698, 0.33),
            WeatherCity("Dallas, TX",       32.7767, -96.7970, 0.30),
            WeatherCity("Austin, TX",       30.2672, -97.7431, 0.19),
            WeatherCity("San Antonio, TX",  29.4241, -98.4936, 0.18),
        ],
    ),
    BalancingAuthority(
        code="CISO",
        name="California ISO",
        market="CAISO",
        timezone="America/Los_Angeles",
        peak_season="Summer",
        cities=[
            WeatherCity("Los Angeles, CA",   34.0522, -118.2437, 0.42),
            WeatherCity("San Francisco, CA", 37.7749, -122.4194, 0.16),
            WeatherCity("San Diego, CA",     32.7157, -117.1611, 0.16),
            WeatherCity("Fresno, CA",        36.7378, -119.7871, 0.13),
            WeatherCity("Sacramento, CA",    38.5816, -121.4944, 0.13),
        ],
    ),
    BalancingAuthority(
        code="ISNE",
        name="ISO New England",
        market="ISO-NE",
        timezone="America/New_York",
        peak_season="Summer",
        cities=[
            WeatherCity("Boston, MA",      42.3601, -71.0589, 0.45),
            WeatherCity("Hartford, CT",    41.7658, -72.6734, 0.20),
            WeatherCity("Providence, RI",  41.8240, -71.4128, 0.15),
            WeatherCity("Manchester, NH",  42.9956, -71.4548, 0.10),
            WeatherCity("Portland, ME",    43.6591, -70.2568, 0.10),
        ],
    ),
    BalancingAuthority(
        code="NYIS",
        name="New York ISO",
        market="NYISO",
        timezone="America/New_York",
        peak_season="Summer",
        cities=[
            WeatherCity("New York, NY",  40.7128, -74.0060, 0.62),
            WeatherCity("Buffalo, NY",   42.8864, -78.8784, 0.12),
            WeatherCity("Albany, NY",    42.6526, -73.7562, 0.10),
            WeatherCity("Rochester, NY", 43.1566, -77.6088, 0.09),
            WeatherCity("Syracuse, NY",  43.0481, -76.1474, 0.07),
        ],
    ),
]

BA_CODES = [ba.code for ba in BALANCING_AUTHORITIES]
BA_BY_CODE = {ba.code: ba for ba in BALANCING_AUTHORITIES}

# ---------------------------------------------------------------------------
# Open-Meteo (weather) -- free, no API key, no account
# ---------------------------------------------------------------------------
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_HOURLY_VARS = "temperature_2m,relative_humidity_2m,wind_speed_10m,cloud_cover"

# The archive (ERA5 reanalysis) lags real time by roughly 5 days, so recent
# hours must come from the forecast endpoint's `past_days` window instead.
# This seam is the single most common silent-gap bug in weather pipelines, which
# is exactly why we name it here.
ARCHIVE_LAG_DAYS = 6
FORECAST_PAST_DAYS = 10   # overlap generously; the transform de-duplicates
FORECAST_AHEAD_DAYS = 3   # forward weather powers the peak-risk early warning

# ---------------------------------------------------------------------------
# Analytical parameters
# ---------------------------------------------------------------------------
# 65 degrees F is the long-standing US utility convention for the temperature at
# which neither heating nor cooling load is triggered. Degree-hours measured
# from this base are the standard weather-normalisation unit in load forecasting.
DEGREE_HOUR_BASE_F = 65.0

# Backfill window. 25 months (not 12) because year-over-year DAX measures need a
# full prior year PLUS the current partial year to compare against.
BACKFILL_MONTHS = 25

# An hour whose absolute percentage error exceeds this is flagged as a
# "material miss" -- the operational alert threshold. 5% is a deliberately
# defensible round number for day-ahead load forecasting, where good BAs run
# 1.5-3% MAPE.
MATERIAL_MISS_APE = 0.05

# Default assumption for the scenario cost of imbalance, $ per MWh of absolute
# error. This is an ASSUMPTION for a what-if slider, never presented as a
# measured saving. Real intraday balancing spreads vary enormously by market.
DEFAULT_IMBALANCE_COST_PER_MWH = 40.0

# ---------------------------------------------------------------------------
# Retry / politeness
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 60
HTTP_MAX_RETRIES = 5
HTTP_BACKOFF_BASE = 2.0   # seconds; exponential
USER_AGENT = "grid-forecast-accuracy/1.0 (portfolio project; contact via GitHub)"
