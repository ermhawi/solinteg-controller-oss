#!/usr/bin/env python3
"""Validate a ChatGPT SE2 outlook, keep local history and publish read-only XC40 JSON.

No network, Home Assistant DB, inverter, charger or car access. Forecast
values are scenarios supplied by the daily research job, not measurements.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SCHEMA = "xc40_se2_price_forecast_v1"
INPUT_SCHEMA = "xc40_se2_price_forecast_input_v1"
DATABASE = Path("/opt/solinteg/ev-shadow-runtime/var/xc40-price-forecasts.sqlite")
OUTPUT = Path("/opt/solinteg/ev-shadow-runtime/public/xc40-price-forecast.json")
RECOMMENDATIONS = {"charge_today", "wait", "no_clear_difference"}
ALLOWED_HOSTS = (
    "elprisetjustnu.se", "nordpoolgroup.com", "svk.se", "entsoe.eu",
    "open-meteo.com", "smhi.se", "energiforetagen.se", "ei.se",
    "energiforsk.se", "tibber.com", "nordpool.no",
)
MAX_INPUT_BYTES = 16384


class ForecastError(ValueError):
    pass


def _number(value: object, low: float, high: float, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ForecastError(f"invalid {name}")
    return round(float(value), 3)


def _string(value: object, limit: int, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ForecastError(f"invalid {name}")
    if any(ord(c) < 32 or c in "<>{}" for c in value) or "@" in value:
        raise ForecastError(f"unsafe {name}")
    return value


def _keys(data: object, expected: set[str], name: str) -> dict:
    if type(data) is not dict or set(data) != expected:
        raise ForecastError(f"invalid fields in {name}")
    return data


def validate(data: dict, *, now: datetime | None = None) -> dict:
    """Fail closed: only a small sanitized, bounded source-attested payload."""
    data = _keys(data, {
        "schema", "area", "local_date", "recommendation", "reason",
        "best_charge_date", "savings_kr_20kwh", "savings_kr_40kwh",
        "days", "sources",
    }, "forecast")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ForecastError("naive current time")
    local_today = now.astimezone(ZoneInfo("Europe/Stockholm")).date()
    if data["schema"] != INPUT_SCHEMA or data["area"] != "SE2" or data["local_date"] != local_today.isoformat():
        raise ForecastError("wrong schema/area or expired forecast date")
    recommendation = data["recommendation"]
    if recommendation not in RECOMMENDATIONS:
        raise ForecastError("invalid recommendation")
    reason = _string(data["reason"], 240, "reason")
    if "://" in reason or any(x in reason.lower() for x in ("token", "password", "apikey", "bearer")):
        raise ForecastError("unsafe reason")

    days = data["days"]
    if type(days) is not list or not 3 <= len(days) <= 7:
        raise ForecastError("expected 3-7 daily price points")
    clean_days = []
    for idx, item in enumerate(days):
        item = _keys(item, {
            "date", "type", "price_ore_kwh", "low_ore_kwh", "high_ore_kwh",
        }, "day")
        day = (local_today + timedelta(days=idx)).isoformat()
        if item["date"] != day or item["type"] not in ("published", "forecast"):
            raise ForecastError("unsorted/nonconsecutive day or invalid type")
        if idx > 1 and item["type"] == "published":
            raise ForecastError("future unauctioned price classified as published")
        price = _number(item["price_ore_kwh"], -250, 1500, "day.price")
        low = _number(item["low_ore_kwh"], -250, 1500, "day.low")
        high = _number(item["high_ore_kwh"], -250, 1500, "day.high")
        if low > price or high < price or (item["type"] == "published" and (low != price or high != price)):
            raise ForecastError("invalid price scenarios")
        clean_days.append({
            "date": day, "type": item["type"],
            "price_ore_kwh": price, "low_ore_kwh": low, "high_ore_kwh": high,
        })
    if not any(day["type"] == "forecast" for day in clean_days):
        raise ForecastError("no forecast observations")

    best = data["best_charge_date"]
    if best is not None and (type(best) is not str or best not in [x["date"] for x in clean_days]):
        raise ForecastError("invalid suggested charge date")
    if recommendation == "wait" and (best is None or best <= local_today.isoformat()):
        raise ForecastError("waiting requires a later recommended day")
    if recommendation == "charge_today" and best != local_today.isoformat():
        raise ForecastError("charge_today requires today's date")

    saving20, saving40 = data["savings_kr_20kwh"], data["savings_kr_40kwh"]
    if (saving20 is None) != (saving40 is None):
        raise ForecastError("incomplete illustrative savings")
    if saving20 is not None:
        saving20 = _number(saving20, 0, 500, "savings20")
        saving40 = _number(saving40, 0, 1000, "savings40")
        if abs(2 * saving20 - saving40) > 0.25:
            raise ForecastError("savings scale inconsistent")

    sources = data["sources"]
    if type(sources) is not list or not 1 <= len(sources) <= 6:
        raise ForecastError("missing referenced data sources")
    clean_sources = []
    for source in sources:
        source = _keys(source, {"name", "url"}, "source")
        label = _string(source["name"], 80, "source name")
        url = _string(source["url"], 280, "source URL")
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in (None, 443) or parts.query or parts.fragment
                or not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_HOSTS)):
            raise ForecastError("unsupported public source URL")
        clean_sources.append({"name": label, "url": url})

    return {
        "schema": SCHEMA, "area": "SE2", "generated_at_utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "local_date": local_today.isoformat(), "recommendation": recommendation,
        "reason": reason, "best_charge_date": best,
        "savings_kr_20kwh": saving20, "savings_kr_40kwh": saving40,
        "savings_basis": "indicative_spot_difference_with_25pct_vat_excludes_network_and_energy_tax",
        "assumed_single_phase_6a_kw": 1.38, "quality": "published_prices_and_uncertain_scenarios",
        "days": clean_days, "sources": clean_sources, "device_writes": 0,
    }


def publish(payload: dict, *, database: Path = DATABASE, output: Path = OUTPUT) -> bool:
    """Atomic JSON publication, idempotent bounded SQLite history, no source DB writes."""
    if os.environ.get("XC40_FORECAST_TEST_OUTPUT") != "1" and (database != DATABASE or output != OUTPUT):
        raise ForecastError("non-production publication paths disabled")
    if not database.parent.is_dir() or not output.parent.is_dir():
        raise ForecastError("publisher directories not installed")
    if database.is_symlink() or output.is_symlink():
        raise ForecastError("refusing symlink destination")
    if database.parent.resolve() == output.parent.resolve():
        raise ForecastError("private history must be separate from published JSON")

    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ForecastError("output too large")
    if output.exists() and output.read_text(encoding="utf-8") == text:
        return False

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    old_umask = os.umask(0o077)
    try:
        with sqlite3.connect(database, timeout=10) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS forecast_runs (
                generated_at_utc TEXT NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL
            )""")
            connection.execute(
                "INSERT OR IGNORE INTO forecast_runs VALUES (?, ?, ?)",
                (payload["generated_at_utc"], digest, text.strip()),
            )
        fd, temp = tempfile.mkstemp(prefix=".xc40-price-forecast-", dir=str(output.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, 0o644)
            os.replace(temp, output)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    finally:
        os.umask(old_umask)
    return True


def make_demo(*, now: datetime | None = None) -> dict:
    """Synthetic, explicitly labelled sample for an end-to-end UI check."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.astimezone(ZoneInfo("Europe/Stockholm")).date()
    prices = [32, 45, 28, 20, 39, 56, 62]
    lows = [32, 45, 18, 12, 22, 33, 35]
    highs = [32, 45, 46, 40, 58, 83, 94]
    return {
        "schema": SCHEMA, "area": "SE2",
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "local_date": today.isoformat(),
        "recommendation": "wait",
        "reason": "TESTDATA: syntetiska priser och laddningsråd, endast för visuell verifiering.",
        "best_charge_date": (today + timedelta(days=3)).isoformat(),
        "savings_kr_20kwh": 6.25, "savings_kr_40kwh": 12.5,
        "savings_basis": "synthetic_demo_only",
        "assumed_single_phase_6a_kw": 1.38,
        "quality": "demo_synthetic",
        "days": [
            {
                "date": (today + timedelta(days=i)).isoformat(),
                "type": "published" if i < 2 else "forecast",
                "price_ore_kwh": prices[i],
                "low_ore_kwh": lows[i],
                "high_ore_kwh": highs[i],
            }
            for i in range(len(prices))
        ],
        "sources": [], "device_writes": 0,
    }


def main() -> None:
    try:
        if sys.argv[1:] == ["--demo"]:
            payload = make_demo()
        elif sys.argv[1:]:
            raise ForecastError("unknown options")
        else:
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ForecastError("input too large")
            payload = validate(json.loads(raw))
        changed = publish(payload)
    except (ValueError, TypeError, sqlite3.Error, OSError) as exc:
        raise SystemExit(f"forecast not published: {exc}") from None
    print(json.dumps({
        "schema": SCHEMA, "published": changed,
        "generated_at_utc": payload["generated_at_utc"],
        "days": len(payload["days"]), "quality": payload["quality"],
        "device_writes": 0,
    }))


if __name__ == "__main__":
    main()
