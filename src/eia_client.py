"""
EIA API v2 client -- hourly demand, day-ahead forecast, generation, interchange.

THE ENDPOINT WE USE
-------------------
    GET https://api.eia.gov/v2/electricity/rto/region-data/data/

Query shape (EIA v2 uses PHP-style bracket arrays, which is unusual and worth
knowing for the interview):

    ?api_key=<KEY>
    &frequency=hourly
    &data[0]=value
    &facets[respondent][]=PJM
    &facets[type][]=D&facets[type][]=DF
    &start=2024-06-01T00
    &end=2024-07-01T00
    &sort[0][column]=period&sort[0][direction]=asc
    &offset=0&length=5000

Response shape:

    { "response": { "total": "17856",
                    "dateFormat": "YYYY-MM-DD\\"T\\"HH24",
                    "data": [ { "period": "2024-06-01T00",
                                "respondent": "PJM",
                                "respondent-name": "PJM Interconnection, LLC",
                                "type": "D",
                                "type-name": "Demand",
                                "value": 95213,
                                "value-units": "megawatthours" }, ... ] } }

TWO THINGS THAT BITE PEOPLE HERE
--------------------------------
1. `length` maxes out at 5000 rows. Ask for more and you get 5000 with no
   warning. Every row past that is silently lost. We therefore ALWAYS page
   using `total` as ground truth and assert we retrieved what was promised.
2. `period` on this dataset is UTC. If you treat it as local time your entire
   hour-of-day analysis is wrong by 4-8 hours, which quietly destroys the
   peak-hour findings. We keep UTC in raw and derive local time in SQL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd

from . import config
from .utils import ApiError, get_logger, http_get_json, month_partitions

log = get_logger("eia_client")

EIA_HOUR_FMT = "%Y-%m-%dT%H"


def _require_key() -> str:
    if not config.EIA_API_KEY:
        raise ApiError(
            "EIA_API_KEY is not set.\n"
            "  1. Get a free key (instant, no approval): "
            "https://www.eia.gov/opendata/register.php\n"
            "  2. Locally:  create a .env file containing  EIA_API_KEY=your_key\n"
            "     then run:  python -m src.ingest  (load_dotenv is called for you)\n"
            "  3. In CI:    add it as GitHub repo secret named EIA_API_KEY"
        )
    return config.EIA_API_KEY


# ---------------------------------------------------------------------------
# Metadata probe -- schema-drift detection
# ---------------------------------------------------------------------------
def probe_route(route: str) -> dict:
    """Ask EIA to describe a route: its frequencies, facets, columns and the
    period range it actually holds.

    WHY THIS MATTERS: "what breaks if the API changes?" is a guaranteed
    interview question. Answer: nothing silently, because this probe runs in CI
    and its output is committed. If EIA renames the `type` facet or drops `DF`
    for a BA, the committed metadata file changes and the diff shows up in git.
    That is schema-drift detection for $0.
    """
    key = _require_key()
    url = f"{config.EIA_BASE}/{route}/"
    payload = http_get_json(url, {"api_key": key}, context=f"probe {route}")
    resp = payload.get("response", {})
    return {
        "route": route,
        "api_version": payload.get("apiVersion"),
        "description": resp.get("description"),
        "frequencies": [f.get("id") for f in resp.get("frequency", []) if isinstance(f, dict)],
        "facets": [f.get("id") for f in resp.get("facets", []) if isinstance(f, dict)],
        "columns": [d.get("id") for d in resp.get("data", {}).values()]
                   if isinstance(resp.get("data"), dict) else list(resp.get("data", [])),
        "start_period": resp.get("startPeriod"),
        "end_period": resp.get("endPeriod"),
    }


def probe_available_respondents(route: str = config.EIA_REGION_DATA_ROUTE) -> list[dict]:
    """List every BA code the route exposes, so we can prove our six exist."""
    key = _require_key()
    url = f"{config.EIA_BASE}/{route}/facet/respondent/"
    payload = http_get_json(url, {"api_key": key}, context="probe respondents")
    return payload.get("response", {}).get("facets", [])


# ---------------------------------------------------------------------------
# Paged data pull
# ---------------------------------------------------------------------------
def _fetch_page(
    route: str,
    *,
    respondents: list[str],
    facet_name: str,
    facet_values: list[str],
    start: datetime,
    end: datetime,
    offset: int,
) -> tuple[list[dict], int]:
    key = _require_key()
    url = f"{config.EIA_BASE}/{route}/data/"
    params: dict[str, Any] = {
        "api_key": key,
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": respondents,
        f"facets[{facet_name}][]": facet_values,
        "start": start.strftime(EIA_HOUR_FMT),
        "end": end.strftime(EIA_HOUR_FMT),
        "sort[0][column]": "period",
        "sort[0][direction]": "asc",
        "offset": offset,
        "length": config.EIA_PAGE_SIZE,
    }
    payload = http_get_json(url, params, context=f"{route} offset={offset}")
    resp = payload.get("response")
    if resp is None:
        raise ApiError(f"{route}: response envelope missing. Keys: {list(payload)}")

    rows = resp.get("data") or []
    # `total` arrives as a string in EIA v2. Coerce defensively.
    try:
        total = int(resp.get("total", len(rows)))
    except (TypeError, ValueError):
        total = len(rows)
    return rows, total


def fetch_series(
    route: str,
    *,
    facet_name: str,
    facet_values: list[str],
    start: datetime,
    end: datetime,
    respondents: list[str] | None = None,
) -> pd.DataFrame:
    """Pull a UTC-bounded window for all BAs, paging until `total` is satisfied.

    Chunking by month keeps each chunk's row count predictable and bounded, so a
    single bad month cannot poison a two-year backfill.
    """
    respondents = respondents or config.BA_CODES
    frames: list[pd.DataFrame] = []
    grand_total = 0

    for chunk_start, chunk_end in month_partitions(start, end):
        offset = 0
        chunk_rows: list[dict] = []
        expected: int | None = None

        while True:
            rows, total = _fetch_page(
                route,
                respondents=respondents,
                facet_name=facet_name,
                facet_values=facet_values,
                start=chunk_start,
                end=chunk_end,
                offset=offset,
            )
            if expected is None:
                expected = total
            chunk_rows.extend(rows)

            if not rows or len(chunk_rows) >= expected or len(rows) < config.EIA_PAGE_SIZE:
                break
            offset += config.EIA_PAGE_SIZE

        # Loud assertion instead of a silent truncation.
        if expected and len(chunk_rows) < expected:
            log.warning(
                "%s %s..%s: retrieved %d of %d promised rows -- possible truncation",
                route, chunk_start.date(), chunk_end.date(), len(chunk_rows), expected,
            )
        log.info("%s %s..%s -> %d rows", route, chunk_start.date(), chunk_end.date(), len(chunk_rows))
        grand_total += len(chunk_rows)
        if chunk_rows:
            frames.append(pd.DataFrame(chunk_rows))

    if not frames:
        log.warning("%s: no rows returned for %s..%s", route, start, end)
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    log.info("%s: %d total rows for %s..%s", route, grand_total, start.date(), end.date())
    return _normalise(df)


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce EIA's loose types into a strict schema.

    EIA returns `value` as a number OR null OR occasionally a string, and
    `period` as 'YYYY-MM-DDTHH'. We convert once, here, so nothing downstream
    ever has to guess. Rows with a null value are KEPT, not dropped -- a missing
    hour is itself a data-quality finding worth showing on the dashboard.
    """
    out = df.copy()
    out["period_utc"] = pd.to_datetime(out["period"], format="%Y-%m-%dT%H", utc=True, errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")

    rename = {
        "respondent": "ba_code",
        "respondent-name": "ba_name_eia",
        "type": "series_type",
        "type-name": "series_name",
        "value-units": "value_units",
        "fueltype": "fuel_code",
        "type-name": "series_name",
    }
    out = out.rename(columns={k: v for k, v in rename.items() if k in out.columns})

    bad_period = out["period_utc"].isna().sum()
    if bad_period:
        log.warning("%d rows had an unparseable period and will be dropped", bad_period)
        out = out[out["period_utc"].notna()]

    keep = [c for c in [
        "period_utc", "ba_code", "ba_name_eia", "series_type", "series_name",
        "fuel_code", "type-name", "value", "value_units",
    ] if c in out.columns]
    return out[keep].drop_duplicates()


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------
def fetch_region_data(start: datetime, end: datetime) -> pd.DataFrame:
    """Demand (D), day-ahead forecast (DF), net generation (NG), interchange (TI)."""
    return fetch_series(
        config.EIA_REGION_DATA_ROUTE,
        facet_name="type",
        facet_values=config.EIA_SERIES_TYPES,
        start=start,
        end=end,
    )


def fetch_fuel_mix(start: datetime, end: datetime) -> pd.DataFrame:
    """Hourly net generation by fuel type, for the renewable-share KPI."""
    return fetch_series(
        config.EIA_FUEL_TYPE_ROUTE,
        facet_name="fueltype",
        facet_values=["SUN", "WND", "WAT", "NG", "COL", "NUC", "OIL", "OTH"],
        start=start,
        end=end,
    )


def default_window(months: int = config.BACKFILL_MONTHS) -> tuple[datetime, datetime]:
    """Backfill window ending 'now', rounded to the hour, in UTC."""
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = (end - timedelta(days=int(months * 30.44))).replace(hour=0)
    return start, end
