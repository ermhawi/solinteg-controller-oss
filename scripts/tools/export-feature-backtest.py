#!/usr/bin/env python3
"""Export a MINIMAL read-only replay dataset from Solinteg's live SQLite telemetry.

Run under the service account that can read telemetry.db. The original DB is
opened SQLite mode=ro; this script NEVER writes to the running telemetry DB.
Only sampled plan inputs/decisions and inverter-derived PV/load readings are copied.
Output permissions 0600: telemetry describes a real household's energy usage.

Example:
  sudo -u solinteg python3 scripts/tools/export-feature-backtest.py \
    --source /opt/solinteg/telemetry.db \
    --out /tmp/solinteg-feature-replay.db \
    --start 2026-08-13 --end 2026-10-16
  # If necessary, grant the invoking user access to the *new replay file only*.
  # Keep the output private and never commit it to Git.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import sqlite3

TZ = ZoneInfo("Europe/Stockholm")
SAMPLE_HOURS = (6, 14, 22)
TOLERANCE_MINUTES = 45


def pick_ids(rows: list[tuple[int, str]], start: date, end: date) -> tuple[list[int], dict[int, int]]:
    """One actually logged plan closest to 06, 14, and 22 Stockholm time per date."""
    by_day: dict[date, list[tuple[int, datetime]]] = defaultdict(list)
    for ident, utc in rows:
        dt = datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(TZ)
        if start <= dt.date() <= end:
            by_day[dt.date()].append((ident, dt))
    ids: list[int] = []
    by_hour = dict.fromkeys(SAMPLE_HOURS, 0)
    for day, plans in sorted(by_day.items()):
        for hour in SAMPLE_HOURS:
            target_minutes = hour * 60 + 5
            ident, dt = min(plans, key=lambda row: abs(row[1].hour * 60 + row[1].minute - target_minutes))
            if abs(dt.hour * 60 + dt.minute - target_minutes) <= TOLERANCE_MINUTES:
                ids.append(ident)
                by_hour[hour] += 1
    return ids, by_hour


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/opt/solinteg/telemetry.db", help="Existing live SQLite DB (read-only)")
    parser.add_argument("--out", required=True, help="New private SQLite file (must not exist)")
    parser.add_argument("--start", default=(datetime.now(TZ).date() - timedelta(days=70)).isoformat())
    parser.add_argument("--end", default=(datetime.now(TZ).date() - timedelta(days=3)).isoformat(),
                        help="Most recent date of a sampled plan; choose >=3 days ago for complete future actuals")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end > datetime.now(TZ).date() - timedelta(days=3):
        parser.error("--end must be at least three days in the past (complete 72h follow-up required)")
    if end < start:
        parser.error("--end is before --start")
    out = Path(args.out).resolve()
    if out == Path(args.source).resolve():
        parser.error("Refusing to overwrite the original telemetry database")
    out.parent.mkdir(parents=True, exist_ok=True)

    # Fail closed if anything already exists at the output path; no silent replacements.
    fd = os.open(str(out), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        db = sqlite3.connect(out)
        try:
            db.execute("PRAGMA journal_mode=DELETE")
            # SQLite URI read-only protects the source even if somebody changes the SQL later.
            db.execute("ATTACH DATABASE ? as live", (f"file:{Path(args.source).resolve()}?mode=ro",))
            rows = db.execute("SELECT id,logged_at FROM live.optimizer_runs").fetchall()
            ids, counts = pick_ids(rows, start, end)
            if not ids:
                raise RuntimeError("No matching optimizer logs for selected dates and local times")
            db.execute("""CREATE TABLE optimizer_runs (
                id INTEGER PRIMARY KEY, logged_at TEXT, price_date TEXT, has_tomorrow INTEGER,
                start_soc_kwh REAL, inputs_json TEXT, dispatch_json TEXT)""")
            sql = """SELECT id,logged_at,price_date,has_tomorrow,start_soc_kwh,
                            inputs_json,dispatch_json FROM live.optimizer_runs WHERE id=?"""
            for ident in ids:
                db.execute("INSERT INTO optimizer_runs VALUES (?,?,?,?,?,?,?)", db.execute(sql, (ident,)).fetchone())
            db.execute("""CREATE TABLE readings AS
                SELECT timestamp,pv_w,house_load_w,soc_kwh,grid_w FROM live.readings""")
            db.execute("CREATE INDEX idx_replay_ts ON readings(timestamp)")
            db.commit()
            count = db.execute("SELECT count(*) FROM readings").fetchone()[0]
            print(f"Private replay DB: {out} ({out.stat().st_size:,} bytes)")
            print(f"Selected plans: {len(ids)}; time windows: {counts}; readings: {count:,}")
            print("No credentials, no live SQLite changes. Output is private (mode 0600).")
        finally:
            db.close()
    except Exception:
        out.unlink(missing_ok=True)  # only the new file which this invocation created
        raise


if __name__ == "__main__":
    main()
