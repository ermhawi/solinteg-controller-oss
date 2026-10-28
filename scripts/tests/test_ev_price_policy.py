"""EV price scheduling uses house optimizer economics; no physical writes."""
import json
import sqlite3
import sys
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))
from ev_price_policy import decide_grid_price, read_ev_session, read_house_price_plan


NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
DEPARTURE = NOW + timedelta(hours=1)


def session(remaining=0.3, departure=DEPARTURE):
    return dict(remaining_ac_kwh=remaining, departure_at_utc=departure)


def plan(buys=(200, 50, 70, 80), *, sell=10, exports=None, forecast=None,
         offset=0, logged_at=NOW):
    exports = exports or (0,) * len(buys)
    forecast = forecast or ("actual",) * len(buys)
    starts = [(NOW + timedelta(minutes=15 * (offset + i)))
              .astimezone(ZoneInfo("Europe/Stockholm"))
              .strftime("%Y-%m-%dT%H:%M:%S") for i in range(len(buys))]
    return dict(
        logged_at=logged_at.isoformat(),
        inputs=[dict(startTime=s, buyPrice=price, sellPrice=sell,
                     priceSource=source) for s, price, source in zip(starts, buys, forecast)],
        dispatch=[dict(startTime=s, solarExportKwh=solar)
                  for s, solar in zip(starts, exports)],
    )


def decide(p=None, s=None, *, now=NOW, **kwargs):
    return decide_grid_price(
        plan() if p is None else p, now=now,
        session=session() if s is None else s, **kwargs,
    )


class GridPriceTests(unittest.TestCase):
    def test_waits_for_cheaper_future_grid_slot(self):
        result = decide()
        self.assertEqual(result.grid_budget_a, 0)
        self.assertEqual(result.buy_ore, 200)
        self.assertIn("later", result.reason)

    def test_buys_in_cheapest_current_slot(self):
        result = decide(plan((40, 150, 160, 180)))
        self.assertEqual(result.grid_budget_a, 6)
        self.assertAlmostEqual(result.planned_grid_kwh, 0.3)
        self.assertEqual(result.sell_ore, 10)

    def test_exported_solar_is_valued_as_opportunity_cost_not_free_grid(self):
        # Current solar is economically attractive; grid top-up must remain off.
        result = decide(plan((220, 80, 90, 95), exports=(0.34, 0, 0, 0), sell=5))
        self.assertEqual(result.grid_budget_a, 0)
        self.assertEqual(result.buy_ore, 220)
        self.assertIn("solar scheduled", result.reason)

    def test_cheap_grid_tops_up_behind_planned_solar(self):
        # Required energy exceeds the current solar kWh and all future power
        # windows at this price are more expensive.
        result = decide(plan((20, 190, 210, 220), exports=(0.08, 0, 0, 0)), s=session(0.3))
        self.assertGreater(result.grid_budget_a, 0)
        self.assertLessEqual(result.grid_budget_a, 6)

    def test_future_low_prices_win_when_energy_need_fits(self):
        p = plan((150, 15, 15, 150))
        self.assertEqual(decide(p, s=session(0.6)).grid_budget_a, 0)

    def test_future_risk_premium_uses_same_named_house_parameter(self):
        p = plan((100, 95, 120, 130), forecast=("actual", "forecast", "actual", "actual"))
        self.assertEqual(decide(p, forecast_risk_ore=0).grid_budget_a, 0)
        self.assertEqual(decide(p, forecast_risk_ore=10).grid_budget_a, 6)

    def test_partial_current_slot_and_deadline(self):
        # Ten minutes left in the current slot (not a free extra 15 min).
        now = NOW + timedelta(minutes=5)
        p = plan((40, 150, 200, 220))
        result = decide(p, now=now, s=session(0.2))
        self.assertEqual(result.grid_budget_a, 6)
        self.assertLess(result.planned_grid_kwh, 0.3)

    def test_selects_correct_current_index_not_always_first(self):
        now = NOW + timedelta(minutes=16)
        p = plan((5, 150, 10, 220))
        self.assertEqual(decide(p, now=now, s=session(0.2)).grid_budget_a, 0)
        p = plan((200, 5, 200, 220))
        self.assertEqual(decide(p, now=now, s=session(0.2)).grid_budget_a, 6)

    def test_insufficient_energy_window_marks_deadline_risk(self):
        result = decide(plan((200, 210, 190, 180)), s=session(3.0))
        self.assertEqual(result.grid_budget_a, 6)
        self.assertIn("infeasible", result.reason)

    def test_missing_current_or_future_window_fails_closed(self):
        self.assertEqual(decide(plan(offset=1)).grid_budget_a, 0)
        self.assertEqual(decide(plan((80,))).grid_budget_a, 0)
        p = plan()
        del p["dispatch"][2]
        self.assertEqual(decide(p).grid_budget_a, 0)
        p = plan()
        p["inputs"][2]["startTime"] = "2026-10-28T12:00:00"
        p["dispatch"][2]["startTime"] = p["inputs"][2]["startTime"]
        self.assertEqual(decide(p).grid_budget_a, 0)

    def test_stale_forecast_current_price_and_nonfinite_values_fail_closed(self):
        self.assertEqual(decide(plan(logged_at=NOW - timedelta(hours=2))).grid_budget_a, 0)
        self.assertEqual(decide(plan(forecast=("forecast", "actual", "actual", "actual"))).grid_budget_a, 0)
        for bad in (float("nan"), None, "unavailable"):
            with self.subTest(bad=bad):
                self.assertEqual(decide(plan((bad, 40, 60, 80))).grid_budget_a, 0)

    def test_missing_session_and_reached_target_do_not_buy_grid(self):
        self.assertEqual(decide_grid_price(plan(), now=NOW, session=None).grid_budget_a, 0)
        self.assertEqual(decide(s=session(0)).grid_budget_a, 0)

    def test_dst_ambiguous_local_slots_do_not_authorize_grid(self):
        now = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)
        p = dict(logged_at=now.isoformat(),
                 inputs=[dict(startTime="2026-10-25T02:00:00", buyPrice=10,
                              sellPrice=1, priceSource="actual")],
                 dispatch=[dict(startTime="2026-10-25T02:00:00", solarExportKwh=0)])
        self.assertEqual(decide_grid_price(p, now=now,
                                         session=session(0.1, now + timedelta(minutes=15))).grid_budget_a, 0)

    def test_read_only_uses_house_optimizer_prices_from_shared_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telemetry.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE optimizer_runs (id INTEGER PRIMARY KEY,"
                           "logged_at TEXT, inputs_json TEXT, dispatch_json TEXT)")
                p = plan((20, 100, 150, 200))
                db.execute("INSERT INTO optimizer_runs (logged_at,inputs_json,dispatch_json)"
                           " VALUES (?,?,?)",
                           (p["logged_at"], json.dumps(p["inputs"]), json.dumps(p["dispatch"])))
            actual = read_house_price_plan(str(path))
            self.assertEqual(decide(actual).grid_budget_a, 6)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM optimizer_runs").fetchone()[0], 1)
            self.assertIsNone(read_house_price_plan(str(Path(tmp) / "not-found.db")))

    def test_ev_session_freshness_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volvo.json"
            record = dict(source="home_assistant", connected=True,
                          observed_at_utc=NOW.isoformat(),
                          departure_at_utc=DEPARTURE.isoformat(),
                          remaining_ac_kwh=0.4)
            path.write_text(json.dumps(record))
            self.assertEqual(read_ev_session(str(path), now=NOW)["remaining_ac_kwh"], 0.4)
            self.assertIsNone(read_ev_session(str(path), now=NOW + timedelta(minutes=21)))
            record["source"] = "unverified"
            path.write_text(json.dumps(record))
            self.assertIsNone(read_ev_session(str(path), now=NOW))
            self.assertIsNone(read_ev_session(None, now=NOW))


if __name__ == "__main__":
    unittest.main()
