"""
Transform: raw Parquet -> star schema, using DuckDB as the SQL engine.

WHY DUCKDB AND NOT A HOSTED DATABASE
------------------------------------
The obvious "portfolio" choice is a free-tier Postgres (Supabase, Neon). It is
the wrong choice here, for a reason worth knowing:

    Power BI Service CANNOT run a scheduled refresh against a cloud PostgreSQL
    database without an on-premises data gateway installed on an always-on
    machine.

So the "free Postgres" architecture quietly requires a computer that never
sleeps -- which is not $0, and not something you can demo from a laptop.

DuckDB sidesteps it entirely. It is an in-process analytical database: no
server, no port, no credentials, no gateway. It reads Parquet directly, speaks
excellent ANSI SQL with full window-function support, and runs inside the
GitHub Actions container for free. The SQL you write here is genuine analytical
SQL -- CTEs, window frames, FULL OUTER JOINs, conditional aggregation -- and it
ports to Postgres or Snowflake nearly unchanged. You lose nothing by not paying.

WHAT BELONGS HERE VS. WHAT BELONGS IN DAX
-----------------------------------------
The dividing line, and be ready to defend it:

    ROW-LEVEL and DETERMINISTIC  -> SQL (here)
        forecast_error_mw, ape, degree hours, local hour, peak-hour flag,
        the rolling 30-day baseline. These have exactly one correct value per
        row regardless of what the user clicks. Computing them once at build
        time is cheaper and testable.

    AGGREGATE and FILTER-DEPENDENT -> DAX (Power BI)
        MAPE, bias, YoY, rolling averages over the *selected* period. These MUST
        recalculate when the user changes a slicer, so they cannot be frozen
        into a column. A pre-computed "MAPE" column would be wrong the instant
        someone filtered to weekdays only.

Getting this boundary right is the difference between a model that is fast and
flexible, and one that is either slow or subtly wrong.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd

from . import config
from .utils import append_run_log, get_logger, utcnow, write_freshness

log = get_logger("transform")

# Execution order matters: dimensions before the facts that reference them,
# facts before the aggregates and checks that read them.
SQL_SEQUENCE = [
    "00_setup.sql",
    "01_dim_ba.sql",
    "02_stg_grid.sql",
    "03_stg_weather.sql",
    "04_fact_load_hour.sql",
    "05_dim_date.sql",
    "06_dim_hour.sql",
    "07_fact_fuel_mix.sql",
    "08_agg_daily_brief.sql",
    "09_dq_checks.sql",
]

# Tables exported for Power BI and the website.
CURATED_TABLES = [
    "dim_ba",
    "dim_date",
    "dim_hour",
    "dim_fuel",
    "fact_load_hour",
    "fact_fuel_mix",
    "agg_daily_brief",
    "dq_checks",
]


# ---------------------------------------------------------------------------
# Seeds -- Python config becomes SQL tables
# ---------------------------------------------------------------------------
def _seed_ba() -> pd.DataFrame:
    """dim_ba's source, generated from src/config.py.

    Why not just type the six rows into SQL? Because then config.py and the SQL
    could drift apart, and the weather weights used at ingest time would no
    longer match the metadata shown on the dashboard. One source of truth.
    """
    return pd.DataFrame([
        {
            "ba_code": ba.code,
            "ba_name": ba.name,
            "market": ba.market,
            "timezone": ba.timezone,
            "peak_season": ba.peak_season,
            "city_count": len(ba.cities),
            "weather_cities": ", ".join(c.name for c in ba.cities),
        }
        for ba in config.BALANCING_AUTHORITIES
    ])


def _seed_holiday(start: str = "2019-01-01", end: str = "2030-12-31") -> pd.DataFrame:
    """US federal holidays.

    WHY HOLIDAYS MATTER SO MUCH HERE: holiday load looks like a Sunday even when
    it falls on a Tuesday. Forecast models handle this badly because there are
    only ~10 examples a year to learn from, so holidays are reliably among the
    worst forecast days. Without this table, every holiday would be misclassified
    as a weekday and would pollute the weekday baseline -- making normal weekdays
    look worse and hiding the real holiday problem.

    pandas ships the US federal calendar, so this costs us one import.
    """
    from pandas.tseries.holiday import USFederalHolidayCalendar

    cal = USFederalHolidayCalendar()
    holidays = cal.holidays(start=start, end=end, return_name=True)
    df = holidays.reset_index()
    df.columns = ["holiday_date", "holiday_name"]
    df["holiday_date"] = pd.to_datetime(df["holiday_date"]).dt.date
    return df


# ---------------------------------------------------------------------------
def _glob(directory: Path) -> str | None:
    files = sorted(directory.glob("*.parquet"))
    if not files:
        return None
    # DuckDB accepts a forward-slash glob on Windows.
    return (directory / "*.parquet").as_posix()


def build(db_path: Path | None = None, strict: bool = True) -> duckdb.DuckDBPyConnection:
    started = utcnow()
    db_path = db_path or (config.DATA_DIR / "warehouse.duckdb")
    if db_path.exists():
        # Rebuild from scratch every run. The whole point of immutable raw is
        # that the warehouse is disposable and reproducible -- if you cannot
        # delete it and get the same thing back, your pipeline is not
        # deterministic and you cannot trust it.
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    try:
        con.execute("INSTALL icu; LOAD icu;")
        log.info("icu extension loaded (timezone-aware AT TIME ZONE available)")
    except Exception as exc:  # noqa: BLE001
        log.warning("icu extension unavailable (%s); DST handling may be approximate", exc)

    # Register seeds
    con.register("seed_ba", _seed_ba())
    con.register("seed_holiday", _seed_holiday())
    log.info("seeds registered: seed_ba (%d rows), seed_holiday", len(config.BALANCING_AUTHORITIES))

    region_glob = _glob(config.RAW_DIR / "region_data")
    weather_glob = _glob(config.RAW_DIR / "weather_city")
    fuel_glob = _glob(config.RAW_DIR / "fuel_mix")

    if not region_glob:
        raise RuntimeError(
            "No raw region_data Parquet found. Run one of:\n"
            "  python -m src.ingest --demo      (synthetic fixture, no key)\n"
            "  python -m src.ingest --backfill  (real data, needs EIA_API_KEY)"
        )
    if not weather_glob:
        raise RuntimeError("No raw weather Parquet found. Run the ingest step first.")

    params = {
        "region_glob": region_glob,
        "weather_glob": weather_glob,
        "fuel_glob": fuel_glob or region_glob,  # placeholder; 07 is skipped if absent
        "degree_base": config.DEGREE_HOUR_BASE_F,
        "material_miss": config.MATERIAL_MISS_APE,
    }

    for name in SQL_SEQUENCE:
        path = config.SQL_DIR / name
        sql = path.read_text(encoding="utf-8")

        if name == "07_fact_fuel_mix.sql" and not fuel_glob:
            # Demo mode does not generate a fuel mix. Create the tables empty so
            # the star schema shape is identical either way and downstream code
            # never needs a "does this table exist?" branch.
            log.warning("no fuel_mix raw data; creating dim_fuel + empty fact_fuel_mix")
            for stmt in _split_statements(sql):
                if "TABLE dim_fuel" in stmt:
                    con.execute(stmt)
            con.execute("""
                CREATE OR REPLACE TABLE fact_fuel_mix (
                    ba_code VARCHAR, period_utc TIMESTAMPTZ, date_key DATE,
                    local_hour INTEGER, fuel_code VARCHAR, generation_mw DOUBLE,
                    total_generation_mw DOUBLE, generation_share DOUBLE
                )""")
            continue

        # DuckDB substitutes named parameters within a SINGLE statement, so the
        # file has to be split before execution.
        statements = _split_statements(sql)
        for stmt in statements:
            used = {k: v for k, v in params.items() if f"${k}" in stmt}
            con.execute(stmt, used) if used else con.execute(stmt)
        log.info("ran %s (%d statements)", name, len(statements))

    _report_and_gate(con, strict=strict)

    rows = con.execute("SELECT COUNT(*) FROM fact_load_hour").fetchone()[0]
    append_run_log("transform", "SUCCESS", started, rows_written=rows)
    return con


def _split_statements(sql: str) -> list[str]:
    """Split a .sql file into executable statements.

    A naive sql.split(';') is wrong and this project proved it the hard way: the
    header comment in 04_fact_load_hour.sql contains a semicolon inside prose,
    so the file was chopped mid-sentence and DuckDB tried to parse English.

    This scanner tracks two states -- inside a single-quoted string literal, and
    inside a '--' line comment -- and only treats a semicolon as a terminator
    when in neither. It also strips comments from the emitted SQL, which keeps
    DuckDB's error messages pointing at real code.

    Note the SQL escape rule: '' (two single quotes) inside a literal is an
    escaped quote, not the end of the string. printf('%02d:00', ...) in the
    narrative SQL depends on getting that right.
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    in_comment = False
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_comment:
            if ch == "\n":
                in_comment = False
                buf.append(ch)
            i += 1
            continue

        if in_string:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":       # escaped quote -> consume both, stay inside
                    buf.append(nxt)
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        # Not in a string or comment
        if ch == "-" and nxt == "-":
            in_comment = True
            i += 2
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def _report_and_gate(con: duckdb.DuckDBPyConnection, strict: bool) -> None:
    """Print the DQ table and stop the build on any hard failure."""
    checks = con.execute(
        "SELECT check_name, severity, result, observed, expected, description FROM dq_checks"
    ).df()

    log.info("---------------- DATA QUALITY ----------------")
    for _, r in checks.iterrows():
        mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}.get(r["result"], "?")
        log.info("  [%-4s] %-28s observed=%-10s expected=%-10s",
                 mark, r["check_name"], r["observed"], r["expected"])
    log.info("----------------------------------------------")

    failures = checks[(checks["severity"] == "FAIL") & (checks["result"] != "PASS")]
    if not failures.empty:
        names = ", ".join(failures["check_name"])
        msg = f"BUILD GATE: hard data-quality failures -> {names}"
        if strict:
            # Failing loudly here is the entire point. A pipeline that publishes
            # broken data is worse than one that stops, because a stopped
            # pipeline gets fixed and a broken one gets believed.
            raise RuntimeError(msg)
        log.error(msg)

    warns = checks[(checks["severity"] == "WARN") & (checks["result"] != "PASS")]
    if not warns.empty:
        log.warning("non-blocking warnings: %s", ", ".join(warns["check_name"]))


# ---------------------------------------------------------------------------
def export(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Write curated tables as BOTH Parquet and CSV.

    WHY BOTH:
      Parquet -- what Power BI should actually consume. ~10x smaller than CSV,
                 carries real data types (no "is this a date or a string?"
                 guessing in Power Query), and loads far faster. Power Query
                 reads it with Parquet.Document(Web.Contents(url)).
      CSV     -- portability and inspection. A recruiter can open it in Excel, a
                 reviewer can eyeball it on GitHub, and it is the universal
                 fallback if anything about the Parquet path misbehaves.

    The extra storage cost is trivial; the flexibility is not.
    """
    counts: dict[str, int] = {}
    for table in CURATED_TABLES:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pq = config.CURATED_DIR / f"{table}.parquet"
        csv = config.CURATED_DIR / f"{table}.csv"

        con.execute(f"COPY {table} TO '{pq.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        # HEADER + explicit UTC timestamp format so Power Query never has to
        # guess a locale. An ISO-8601 string with an offset is unambiguous in
        # every regional setting, which matters because Power BI Service runs in
        # a different locale than your laptop.
        con.execute(
            f"COPY {table} TO '{csv.as_posix()}' "
            "(FORMAT CSV, HEADER, DELIMITER ',', TIMESTAMPFORMAT '%Y-%m-%dT%H:%M:%S%z')"
        )

        counts[table] = n
        size_mb = pq.stat().st_size / 1e6
        csv_mb = csv.stat().st_size / 1e6
        log.info("exported %-18s %8d rows | parquet %6.2f MB | csv %6.2f MB",
                 table, n, size_mb, csv_mb)

    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build the star schema from raw Parquet")
    p.add_argument("--no-strict", action="store_true",
                   help="log hard DQ failures instead of stopping the build")
    p.add_argument("--no-export", action="store_true", help="build only, skip file export")
    args = p.parse_args(argv)

    con = build(strict=not args.no_strict)
    if not args.no_export:
        counts = export(con)
        write_freshness({
            **{k: v for k, v in (con.execute(
                "SELECT 'latest_actual_hour_utc' AS k, MAX(period_utc)::VARCHAR AS v "
                "FROM fact_load_hour WHERE row_kind='actual'"
            ).fetchall())},
            "curated_row_counts": counts,
        })
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
