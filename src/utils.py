"""
Shared plumbing: resilient HTTP, structured logging, and the run ledger.

WHY THIS FILE EXISTS
--------------------
An unattended pipeline that runs 24 times a day will, statistically, hit a
transient 503 or a socket timeout every few days. If a single failed call kills
the run, the dashboard silently goes stale and nobody notices for a week. That
is the #1 way portfolio "live dashboards" die.

Three defences live here:
  1. Exponential-backoff retry with jitter, so we survive blips.
  2. A run ledger (data/logs/run_log.csv) recording every run's outcome, so
     failure is *visible* and can be charted inside Power BI itself.
  3. A freshness stamp (data/logs/freshness.json) that the dashboard reads to
     display "data as of ..." -- turning trust into a first-class KPI.
"""

from __future__ import annotations

import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from . import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"


def get_logger(name: str) -> logging.Logger:
    """One configured logger per module. Logs go to stdout so GitHub Actions
    captures them in the job output -- free observability with zero tooling."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


log = get_logger("utils")


# ---------------------------------------------------------------------------
# Resilient HTTP
# ---------------------------------------------------------------------------
class ApiError(RuntimeError):
    """Raised when a request fails after all retries, or returns a payload we
    cannot interpret. Distinct from requests' own exceptions so callers can tell
    'the network flaked' apart from 'the API contract changed'."""


def _is_retryable(status: int) -> bool:
    # 429 = rate limited, 5xx = server-side. Never retry a 4xx like 403 (bad
    # API key) or 400 (bad parameters) -- retrying those just wastes quota and
    # hides a real bug from you.
    return status == 429 or 500 <= status < 600


def http_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    max_retries: int = config.HTTP_MAX_RETRIES,
    timeout: int = config.HTTP_TIMEOUT,
    context: str = "",
) -> dict:
    """GET a JSON document, retrying transient failures with exponential backoff.

    The backoff includes random jitter. Without jitter, if several parallel
    requests fail at the same moment they all retry at the same moment and
    hammer the API in lockstep -- a self-inflicted thundering herd.
    """
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    last_err: str = ""

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_err = f"transport error: {exc.__class__.__name__}: {exc}"
            if attempt == max_retries:
                break
            sleep_s = config.HTTP_BACKOFF_BASE ** attempt + random.uniform(0, 1.0)
            log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                        context or url, attempt, max_retries, last_err, sleep_s)
            time.sleep(sleep_s)
            continue

        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise ApiError(
                    f"{context or url}: HTTP 200 but body is not JSON ({exc}). "
                    f"First 300 chars: {resp.text[:300]!r}"
                ) from exc

        # Non-200
        body_snippet = resp.text[:400]
        last_err = f"HTTP {resp.status_code}: {body_snippet}"

        if not _is_retryable(resp.status_code):
            raise ApiError(f"{context or url}: non-retryable {last_err}")

        if attempt == max_retries:
            break

        # Honour Retry-After when the server tells us how long to wait.
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            sleep_s = float(retry_after)
        else:
            sleep_s = config.HTTP_BACKOFF_BASE ** attempt + random.uniform(0, 1.0)
        log.warning("%s attempt %d/%d -> %s; retrying in %.1fs",
                    context or url, attempt, max_retries, last_err, sleep_s)
        time.sleep(sleep_s)

    raise ApiError(f"{context or url}: exhausted {max_retries} attempts. Last error: {last_err}")


# ---------------------------------------------------------------------------
# Run ledger + freshness stamp
# ---------------------------------------------------------------------------
RUN_LOG_PATH = config.LOG_DIR / "run_log.csv"
FRESHNESS_PATH = config.LOG_DIR / "freshness.json"

_RUN_LOG_HEADER = "run_started_utc,run_finished_utc,job,status,rows_written,duration_sec,message\n"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def append_run_log(
    job: str,
    status: str,
    started: datetime,
    rows_written: int = 0,
    message: str = "",
) -> None:
    """Append one line to the run ledger.

    Why a CSV and not a proper logging service: it is free, it is versioned by
    git alongside the data, and -- the clever part -- Power BI can load it as a
    dimension so the dashboard can display its own pipeline health. Very few
    portfolio projects show pipeline reliability inside the report.
    """
    finished = utcnow()
    duration = (finished - started).total_seconds()
    safe_msg = message.replace(",", ";").replace("\n", " ")[:300]

    if not RUN_LOG_PATH.exists():
        RUN_LOG_PATH.write_text(_RUN_LOG_HEADER, encoding="utf-8")

    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{iso(started)},{iso(finished)},{job},{status},"
            f"{rows_written},{duration:.1f},{safe_msg}\n"
        )
    log.info("run_log <- %s status=%s rows=%s duration=%.1fs", job, status, rows_written, duration)


def write_freshness(payload: dict) -> None:
    """Record how current the data is. The dashboard surfaces this so a viewer
    can never mistake stale data for reality."""
    payload = {"generated_utc": iso(utcnow()), **payload}
    FRESHNESS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_freshness() -> dict:
    if FRESHNESS_PATH.exists():
        return json.loads(FRESHNESS_PATH.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def month_partitions(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Split a date range into month-sized chunks.

    Why: EIA caps a response at 5000 rows. Six BAs x 4 series x 744 hours is
    ~17,856 rows in a long month, so a naive single request would silently
    truncate. Chunking by month and paging within each chunk makes the volume
    predictable and the failure mode obvious.
    """
    out: list[tuple[datetime, datetime]] = []
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end:
        if cursor.month == 12:
            nxt = cursor.replace(year=cursor.year + 1, month=1)
        else:
            nxt = cursor.replace(month=cursor.month + 1)
        out.append((max(cursor, start), min(nxt, end)))
        cursor = nxt
    return out
