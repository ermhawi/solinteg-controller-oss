"""Private read-only historical price evidence, UTC DST folds and immutability."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import ev_cost_price_capture as price

STOCKHOLM = ZoneInfo("Europe/Stockholm")
UTC = timezone.utc


def source_for(day: str, *, buy: float = 123.45, sell: float = 16.71) -> str:
    """A real local day has 92, 96 or 100 UTC slots, including DST folds."""
    d = date.fromisoformat(day)
    start = datetime.combine(d, time(), tzinfo=STOCKHOLM).astimezone(UTC)
    end = datetime.combine(d + timedelta(days=1), time(), tzinfo=STOCKHOLM).astimezone(UTC)
    slots = []
    while start < end:
        after = start + timedelta(minutes=15)
        slots.append({
            "startTime": start.astimezone(STOCKHOLM).strftime("%Y-%m-%dT%H:%M:%S"),
            "endTime": after.astimezone(STOCKHOLM).strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "actual",
            "priceIncludingTaxAndSurcharge": buy,
            "price": sell,
        })
        start = after
    return json.dumps(slots)


class PriceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = str(Path(self.temp.name) / "telemetry.db")
        self.dest = str(Path(self.temp.name) / "price-evidence.db")
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE price_snapshots("
                       "date TEXT PRIMARY KEY, logged_at TEXT, prices_json TEXT)")
            for day in ("2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27"):
                db.execute("INSERT INTO price_snapshots VALUES (?,?,?)",
                           (day, "2026-10-28T08:15:00Z", source_for(day)))

    def tearDown(self):
        self.temp.cleanup()

    def test_ordinary_local_day_is_96_contiguous_unique_utc_slots(self):
        rows = price.normalize_day(
            "2026-10-27", "2026-10-28T08:15:00Z", source_for("2026-10-27"))
        self.assertEqual(len(rows), 96)
        self.assertEqual(rows[0]["buy_before_grid_ore"], "123.45")
        self.assertEqual(rows[0]["sell_model_ore"], "16.71")
        self.assertEqual(rows[0]["quality"], price.QUALITY)
        self.assertEqual([r["slot_start_epoch_ms"] for r in rows],
                         sorted(set(r["slot_start_epoch_ms"] for r in rows)))
        self.assertTrue(all(rows[i]["slot_end_epoch_ms"]
                            == rows[i + 1]["slot_start_epoch_ms"]
                            for i in range(95)))

    def test_fall_back_repeated_hour_has_100_unique_utc_intervals(self):
        rows = price.normalize_day(
            "2026-10-25", "2026-10-26T00:00:00Z", source_for("2026-10-25"))
        self.assertEqual(len(rows), 100)
        self.assertEqual(len({row["slot_start_epoch_ms"] for row in rows}), 100)
        self.assertEqual(rows[-1]["slot_end_epoch_ms"]-rows[0]["slot_start_epoch_ms"],
                         25 * 3600 * 1000)
        # The repeated 02:xx hours are two different four-slot ranges, not duplicates.
        self.assertEqual(rows[8]["slot_start_epoch_ms"] + 4 * 3600 * 1000,
                         rows[24]["slot_start_epoch_ms"])

    def test_spring_forward_day_has_92_slots_and_no_fictitious_local_hour(self):
        rows = price.normalize_day(
            "2026-03-29", "2026-03-30T00:00:00Z", source_for("2026-03-29"))
        self.assertEqual(len(rows), 92)
        self.assertEqual(rows[-1]["slot_end_epoch_ms"]-rows[0]["slot_start_epoch_ms"],
                         23 * 3600 * 1000)

    def test_reject_incomplete_forecast_price_bad_order_and_corrupted_slot(self):
        base = json.loads(source_for("2026-10-27"))
        for bad in (
            base[:-1],
            [{**base[0], "source": "forecast"}, *base[1:]],
            [base[1], base[0], *base[2:]],
            [{**base[0], "endTime": "2026-10-27T01:00:00"}, *base[1:]],
            [{**base[0], "price": float("nan")}, *base[1:]],
            [{**base[0], "priceIncludingTaxAndSurcharge": None}, *base[1:]],
        ):
            with self.subTest(first=bad[0]), self.assertRaises(price.PriceEvidenceError):
                price.normalize_day(
                    "2026-10-27", "2026-10-28T08:15:00Z", json.dumps(bad))

    def test_historical_reader_uses_only_complete_actual_days_and_is_readonly(self):
        rows, missing = price.historical_slots(self.source, today=date(2026, 10, 28),
                                                days=5)
        self.assertEqual(len(rows), 96 * 3 + 100)
        self.assertEqual(missing, ["2026-10-28"])
        self.assertFalse(Path(self.dest).exists())
        with sqlite3.connect(self.source) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM price_snapshots")
                             .fetchone()[0], 4)

    def test_insert_is_private_atomic_and_idempotent(self):
        rows = price.normalize_day(
            "2026-10-27", "2026-10-28T08:15:00Z", source_for("2026-10-27"))
        self.assertEqual(price.append(self.dest, rows), 96)
        self.assertEqual(price.append(self.dest, rows), 0)
        self.assertEqual(Path(self.dest).stat().st_mode & 0o777, 0o600)
        with sqlite3.connect("file:" + self.dest + "?mode=ro", uri=True) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM ev_cost_price_evidence").fetchone()[0], 96)
        self.assertIsNone(price.summary(rows, [])["vehicle_cost_ore"])
        self.assertIsNone(price.summary(rows, [])["invoiced_buy_price_ore"])

    def test_changed_old_price_rejected_without_overwriting(self):
        rows = price.normalize_day(
            "2026-10-27", "2026-10-28T08:15:00Z", source_for("2026-10-27"))
        price.append(self.dest, rows)
        changed = [dict(row) for row in rows]
        changed[0]["buy_before_grid_ore"] = "130"
        with self.assertRaisesRegex(price.PriceEvidenceError, "changed"):
            price.append(self.dest, changed)
        with sqlite3.connect(self.dest) as db:
            self.assertEqual(db.execute(
                "SELECT buy_before_grid_ore FROM ev_cost_price_evidence "
                "WHERE slot_start_epoch_ms=?", (rows[0]["slot_start_epoch_ms"],)
            ).fetchone()[0], "123.45")

    def test_source_contract_and_no_inverter_or_EVCC_calls(self):
        source = Path(price.__file__).read_text()
        for forbidden in ("import evcc_ascii", "import inverter_control",
                          "import modbus_poller", "requests.post",
                          "socket.create_connection", "FC10"):
            self.assertNotIn(forbidden, source)
        for key in ("modelled_day_ahead", "invoiced_buy_price_ore",
                    "vehicle_cost_ore", "device_writes"):
            self.assertIn(key, source)


if __name__ == "__main__":
    unittest.main()
