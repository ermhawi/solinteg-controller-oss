"""Pure-ish, read-only freshness checks extracted from healthcheck.py (T04).

The original healthcheck module still owns environment thresholds, SQLite
query error handling, notification priorities and de-duplication. Passing those
dependencies in keeps monkeypatch contracts and alert fingerprints unchanged.
"""
from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Callable, Any


def poller_stale(con: sqlite3.Connection, now: datetime, *,
                 query: Callable[..., Any], high_priority: int,
                 stale_seconds: int):
    latest = query(con, "SELECT MAX(timestamp) FROM readings")
    if latest is None:
        return ("poller_no_data", high_priority, "Solinteg: no inverter readings yet",
                "The readings table is empty — has solinteg-poller ever run successfully?")
    age = (now - datetime.fromisoformat(latest)).total_seconds()
    if age > stale_seconds:
        return ("poller_stale", high_priority, "Solinteg: inverter poller looks dead",
                f"Last reading was {age:.0f}s ago (expected every 30s). "
                f"Check solinteg-poller on the NUC.")
    return None


def weather_stale(con: sqlite3.Connection, now: datetime, *,
                  query: Callable[..., Any], low_priority: int,
                  stale_seconds: int):
    # No weather station -> suppress the alert but preserve the check key so
    # the caller's resolve sweep can clear an earlier issue.
    if stale_seconds <= 0:
        return None
    latest = query(con, "SELECT MAX(timestamp) FROM weather")
    if latest is None:
        return ("weather_no_data", low_priority, "Solinteg: no weather data yet",
                "The weather table is empty — has solinteg-weather ever run successfully?")
    age = (now - datetime.fromisoformat(latest)).total_seconds()
    if age > stale_seconds:
        return ("weather_stale", low_priority, "Solinteg: weather data is stale",
                f"Last weather reading was {age:.0f}s ago. Non-urgent — the solar forecast "
                f"just falls back to climatology until this recovers. Check solinteg-weather "
                f"and the Ecowitt station/cloud API.")
    return None
