"""Deterministic conflict/precedence matrix for installed-identical energy policy.

Source-only: all measurements are in-memory, no real broker or inverter.
The dispatcher has additional EV/L3/SoC gates and is qualified separately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from energy_priority_policy import SolarState, resolve  # noqa: E402


NOW = datetime(2026, 11, 7, 9, tzinfo=timezone.utc)


def samples(pv: float, house: float, battery: float, grid: float) -> list[dict]:
    return [
        {"timestamp": (NOW - timedelta(seconds=seconds)).isoformat(),
         "pv_w": pv, "house_load_w": house, "battery_w": battery,
         "grid_w": grid, "work_mode_raw": 0x0303}
        for seconds in (60, 30)
    ]


def decision(action: str, power: float, *, soc: float, reserve=20.0,
             pv=2000.0, house=900.0, battery=300.0, grid=1500.0,
             ev=False, recovery=False, state=None):
    return resolve(
        action, power, now=NOW,
        readings=samples(pv, house, battery, grid),
        state=state or SolarState(), soc_pct=soc, reserve_pct=reserve,
        save_w=50.0, recovery=recovery, ev_charging=ev)


class PriorityConflictMatrixTests(unittest.TestCase):
    def test_hard_recovery_and_planned_charge_never_displaced_by_solar(self):
        matrix = [
            ("recovery", {"action": "discharge", "power": 500, "soc": 13, "recovery": True},
             "soc_recovery", "discharge", 500),
            ("planned_charge", {"action": "charge", "power": 1500, "soc": 16},
             "planned_charge", "charge", 1500),
        ]
        for name, kwargs, winner, action, power in matrix:
            with self.subTest(name=name):
                d = decision(kwargs.pop("action"), kwargs.pop("power"), **kwargs)
                self.assertEqual((d.winner, d.action, d.power_w),
                                 (winner, action, power))

    def test_ev_unknown_uses_economic_save_without_claiming_ev_off(self):
        for original in ("discharge", "idle"):
            with self.subTest(original=original):
                d = decision(original, 500 if original == "discharge" else 0,
                             soc=16, ev=None)
                self.assertEqual(d.winner, "unverified_ev")
                self.assertEqual((d.action, d.power_w), ("discharge", 50))

    def test_ev_charging_soft_save_only_applies_below_economic_reserve(self):
        below = decision("discharge", 650, soc=16, ev=True)
        self.assertEqual(below.winner, "ev_grid_priority")
        self.assertEqual((below.action, below.power_w), ("discharge", 50))
        above = decision("discharge", 650, soc=55, ev=True)
        self.assertEqual(above.winner, "economic_plan")
        self.assertEqual((above.action, above.power_w), ("discharge", 650))
        # The higher-level dispatcher must independently guard battery-to-car.

    def test_solar_capture_prioritizes_battery_over_export(self):
        d = decision("discharge", 650, soc=16, ev=False)
        self.assertEqual(d.winner, "solar_capture")
        self.assertEqual(d.action, "idle")
        self.assertEqual(d.power_w, 0)

    def test_solar_capture_above_reserve_does_not_invent_rule(self):
        d = decision("discharge", 650, soc=50, ev=False)
        self.assertEqual(d.winner, "economic_plan")
        self.assertEqual(d.action, "discharge")

    def test_missing_or_stale_pv_samples_retains_plan(self):
        with self.subTest("missing"):
            d = resolve("discharge", 350, now=NOW, readings=[],
                        state=SolarState(), soc_pct=16, reserve_pct=20,
                        save_w=50, recovery=False, ev_charging=False)
            self.assertEqual(d.winner, "unverified_solar")
            self.assertEqual((d.action, d.power_w), ("discharge", 50))
        with self.subTest("no-net-export"):
            d = decision("discharge", 350, soc=16, pv=600, house=900,
                         battery=0, grid=0)
            self.assertEqual(d.winner, "economic_reserve")
            self.assertEqual((d.action, d.power_w), ("discharge", 50))

    def test_save_mode_does_not_force_unnecessary_battery_cycle(self):
        d = decision("discharge", 50, soc=16, ev=False)
        self.assertEqual(d.winner, "solar_capture")
        self.assertEqual(d.action, "idle")
        d = decision("idle", 0, soc=55, pv=100, house=900,
                     grid=-800, battery=0, ev=False)
        self.assertEqual(d.action, "idle")

    def test_same_measurements_produce_reproducible_decisions(self):
        baseline = decision("discharge", 150, soc=16, ev=False)
        for _ in range(80):
            other = decision("discharge", 150, soc=16, ev=False)
            self.assertEqual((other.action, other.power_w, other.winner),
                             (baseline.action, baseline.power_w, baseline.winner))


if __name__ == "__main__":
    unittest.main()
