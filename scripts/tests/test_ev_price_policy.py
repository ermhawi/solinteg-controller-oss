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
from ev_price_policy import (decide_grid_price, read_ev_session,
                             read_house_price_plan, read_shadow_ev_session)


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
        self.assertIn("solar allocated", result.reason)
        self.assertTrue(result.solar_allowed)

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

    def test_deadline_prioritizes_80_even_when_price_is_very_high(self):
        urgent = session(0.3, NOW + timedelta(minutes=10))
        p = plan((2900, 20, 20, 20))
        result = decide(p, s=urgent)
        self.assertEqual(result.charge_mode, "deadline")
        self.assertEqual(result.grid_budget_a, 6)
        self.assertTrue(result.solar_allowed)
        self.assertIn("deadline urgent", result.reason)

    def test_deadline_urgent_uses_grid_with_missing_prices_but_not_invalid_inputs(self):
        urgent = session(0.3, NOW + timedelta(minutes=10))
        self.assertEqual(decide_grid_price(
            None, now=NOW, session=urgent).grid_budget_a, 6)
        self.assertEqual(decide(
            plan(logged_at=NOW - timedelta(hours=3)), s=urgent).grid_budget_a, 6)
        # Corrupt/malformed price data must NOT be treated as a mere outage.
        p = plan((20, 200, 200, 200))
        p["inputs"][0]["buyPrice"] = float("nan")
        self.assertEqual(decide(p, s=urgent).grid_budget_a, 0)
        self.assertEqual(decide_grid_price(
            None, now=NOW, session=session(0.1)).grid_budget_a, 0)

    def test_economy_without_departure_charges_in_cheap_hours_only(self):
        s = dict(charge_mode="economy", remaining_ac_kwh=1.2,
                 departure_at_utc=None)
        cheap_now = plan((40,) + (160,) * 31, sell=10)
        result = decide(cheap_now, s=s)
        self.assertEqual(result.charge_mode, "economy")
        self.assertEqual(result.grid_budget_a, 6)
        self.assertEqual(result.buy_ore, 40)
        self.assertIsNotNone(result.cheap_threshold_ore)
        high_now = plan((160,) + (40,) * 31, sell=10)
        result = decide(high_now, s=s)
        self.assertEqual(result.grid_budget_a, 0)
        self.assertTrue(result.solar_allowed)
        self.assertIn("economy:", result.reason)

    def test_economy_does_not_arbitrarily_call_flat_expensive_power_cheap(self):
        s = dict(charge_mode="economy", remaining_ac_kwh=0.7)
        self.assertEqual(decide(plan((175,) * 32), s=s).grid_budget_a, 0)
        # Free/negative energy is fine even if all slots have the same price.
        self.assertEqual(decide(plan((0,) * 32), s=s).grid_budget_a, 6)

    def test_economy_absolute_ceiling_is_optional_and_has_priority(self):
        p = plan((40,) + (160,) * 31, sell=10)
        s = dict(charge_mode="economy", remaining_ac_kwh=1.2,
                 max_buy_ore_per_kwh=30)
        result = decide(p, s=s)
        self.assertEqual(result.grid_budget_a, 0)
        self.assertTrue(result.solar_allowed)
        self.assertEqual(result.cheap_threshold_ore, 30)
        s["max_buy_ore_per_kwh"] = -5
        result = decide(p, s=s)
        self.assertEqual(result.grid_budget_a, 0)
        self.assertFalse(result.solar_allowed)
        s["max_buy_ore_per_kwh"] = 50
        self.assertEqual(decide(p, s=s).grid_budget_a, 6)

    def test_economy_waits_for_enough_price_forecast_and_fresh_data(self):
        s = dict(charge_mode="economy", remaining_ac_kwh=1.0)
        self.assertEqual(decide(plan((40,) * 8), s=s).grid_budget_a, 0)
        self.assertEqual(decide_grid_price(None, now=NOW, session=s).grid_budget_a, 0)
        self.assertEqual(decide(plan((40,) + (160,) * 31,
                                      logged_at=NOW - timedelta(hours=2)),
                                s=s).grid_budget_a, 0)
        # Economy NEVER invents a deadline to justify expensive grid charging.
        self.assertEqual(decide(plan((160,) + (40,) * 31), s=s).grid_budget_a, 0)

    def test_both_modes_stop_when_80_percent_target_reached(self):
        for mode in ("deadline", "economy"):
            with self.subTest(mode=mode):
                s = dict(charge_mode=mode, remaining_ac_kwh=0,
                         departure_at_utc=DEPARTURE if mode == "deadline" else None)
                decision = decide(plan((0,) * 32), s=s)
                self.assertEqual(decision.grid_budget_a, 0)
                self.assertFalse(decision.solar_allowed)
                self.assertIn("target reached", decision.reason)

    def test_read_ev_session_accepts_no_deadline_only_in_economy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volvo.json"
            record = dict(source="home_assistant", connected=True,
                          observed_at_utc=NOW.isoformat(),
                          charge_mode="economy", target_soc_pct=80,
                          vehicle_soc_pct=67, remaining_ac_kwh=10.0)
            path.write_text(json.dumps(record))
            s = read_ev_session(str(path), now=NOW)
            self.assertEqual(s["charge_mode"], "economy")
            self.assertIsNone(s["departure_at_utc"])
            record["charge_mode"] = "deadline"
            path.write_text(json.dumps(record))
            self.assertIsNone(read_ev_session(str(path), now=NOW))
            record["charge_mode"] = "economy"
            record["target_soc_pct"] = 85
            path.write_text(json.dumps(record))
            self.assertIsNone(read_ev_session(str(path), now=NOW))
            record["target_soc_pct"] = 80
            record["vehicle_soc_pct"] = 81
            path.write_text(json.dumps(record))
            self.assertIsNone(read_ev_session(str(path), now=NOW))
            record["vehicle_soc_pct"] = 80
            record["remaining_ac_kwh"] = 0
            path.write_text(json.dumps(record))
            self.assertEqual(read_ev_session(str(path), now=NOW)["remaining_ac_kwh"], 0)
            record["charge_mode"] = "unknown"
            path.write_text(json.dumps(record))
            self.assertIsNone(read_ev_session(str(path), now=NOW))

    def test_shadow_scenarios_use_separate_parser_and_never_pass_control_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow.json"
            record = dict(
                source="home_assistant_shadow", shadow_only=True,
                physical_control_eligible=False, api_fetch_confirmed=True,
                energy_estimate_unverified=True,
                observation_time_semantics="api_fetch_not_bms_sample",
                oem_timestamp_role="last_changed_not_sampled",
                observation_quality="api_recent_field_change_unverified",
                observed_at_utc=NOW.isoformat(), connected=True,
                charge_mode="economy", target_soc_pct=80, vehicle_soc_pct=63,
                assumed_usable_capacity_kwh=78, assumed_ac_efficiency=0.9,
                remaining_ac_kwh=14.733, max_buy_ore_per_kwh=85,
            )
            path.write_text(json.dumps(record))
            parsed = read_shadow_ev_session(str(path), now=NOW)
            self.assertEqual(parsed["remaining_ac_kwh"], 14.733)
            self.assertEqual(parsed["charge_mode"], "economy")
            self.assertFalse(parsed["physical_control_eligible"])
            self.assertIsNone(parsed["departure_at_utc"])
            self.assertIsNone(read_ev_session(str(path), now=NOW))
            self.assertIsNone(read_shadow_ev_session(
                str(path), now=NOW + timedelta(minutes=6)))

            record["charge_mode"] = "deadline"
            record["departure_at_utc"] = DEPARTURE.isoformat()
            path.write_text(json.dumps(record))
            self.assertEqual(read_shadow_ev_session(
                str(path), now=NOW)["departure_at_utc"], DEPARTURE)

    def test_shadow_parser_rejects_tampering_unverified_or_mismatched_energy(self):
        baseline = dict(
            source="home_assistant_shadow", shadow_only=True,
            physical_control_eligible=False, api_fetch_confirmed=True,
            energy_estimate_unverified=True,
            observation_time_semantics="api_fetch_not_bms_sample",
            oem_timestamp_role="last_changed_not_sampled",
            observation_quality="api_recent_field_change_unverified",
            observed_at_utc=NOW.isoformat(), connected=True,
            charge_mode="economy", target_soc_pct=80,
            vehicle_soc_pct=63, assumed_usable_capacity_kwh=78,
            assumed_ac_efficiency=0.9, remaining_ac_kwh=14.733,
        )
        bad = [
            ("source", "home_assistant"),
            ("physical_control_eligible", True),
            ("shadow_only", False),
            ("api_fetch_confirmed", False),
            ("energy_estimate_unverified", False),
            ("observation_time_semantics", "verified_bms_sample"),
            ("oem_timestamp_role", "measured_at"),
            ("observation_quality", "verified"),
            ("connected", False),
            ("target_soc_pct", 85),
            ("vehicle_soc_pct", 103),
            ("remaining_ac_kwh", 99),
            ("remaining_ac_kwh", float("nan")),
            ("assumed_ac_efficiency", 1.5),
            ("assumed_usable_capacity_kwh", 180),
            ("charge_mode", "unknown"),
            ("departure_at_utc", DEPARTURE.isoformat()),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scenario.json"
            for key, value in bad:
                with self.subTest(key=key, value=value):
                    path.write_text(json.dumps({**baseline, key: value}))
                    self.assertIsNone(read_shadow_ev_session(str(path), now=NOW),
                                      msg=f"tampered {key}")

    def test_shadow_parser_80_percent_target_stops_price_advice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow.json"
            record = dict(
                source="home_assistant_shadow", shadow_only=True,
                physical_control_eligible=False, api_fetch_confirmed=True,
                energy_estimate_unverified=True,
                observation_time_semantics="api_fetch_not_bms_sample",
                oem_timestamp_role="last_changed_not_sampled",
                observation_quality="api_recent_fields_changed_recently",
                observed_at_utc=NOW.isoformat(), connected=True,
                charge_mode="economy", target_soc_pct=80,
                vehicle_soc_pct=80, assumed_usable_capacity_kwh=78,
                assumed_ac_efficiency=0.9, remaining_ac_kwh=0,
            )
            path.write_text(json.dumps(record))
            scenario = read_shadow_ev_session(str(path), now=NOW)
            self.assertEqual(scenario["remaining_ac_kwh"], 0)
            self.assertEqual(decide(plan((0,) * 32), s=scenario).grid_budget_a, 0)


if __name__ == "__main__":
    unittest.main()
