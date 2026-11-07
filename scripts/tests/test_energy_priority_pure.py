"""Pure policy contract; no dispatcher import or physical communication."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from energy_priority_policy import SolarState, resolve


NOW = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)


def sample(t: datetime) -> dict:
    return {"timestamp": t.isoformat(), "pv_w": 1600, "house_load_w": 800,
            "battery_w": 280, "grid_w": 1080, "work_mode_raw": 0x303}


class PureEnergyPriorityTests(unittest.TestCase):
    def decide(self, *, ev=False, action="discharge", watts=50, recovery=False):
        return resolve(action, watts, now=NOW, readings=[
            sample(NOW - timedelta(seconds=60)),
            sample(NOW - timedelta(seconds=30)),
        ], state=SolarState(), soc_pct=16, reserve_pct=20, save_w=50,
            recovery=recovery, ev_charging=ev)

    def test_stable_solar_capture_without_modbus(self):
        result = self.decide()
        self.assertEqual((result.action, result.power_w, result.winner),
                         ("idle", 0, "solar_capture"))

    def test_unverified_ev_does_not_enable_new_general_transition(self):
        result = self.decide(ev=None)
        self.assertEqual((result.action, result.winner), ("discharge", "unverified_ev"))

    def test_recovery_priority_cannot_be_demoted(self):
        result = self.decide(ev=True, action="charge", watts=300, recovery=True)
        self.assertEqual((result.action, result.power_w, result.winner),
                         ("charge", 300, "soc_recovery"))


if __name__ == "__main__":
    unittest.main()
