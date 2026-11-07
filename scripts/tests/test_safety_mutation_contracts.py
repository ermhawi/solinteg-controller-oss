"""K05: prove independent safety contracts reject in-memory Python mutants.

Mutate isolated AST *functions* only; never edit the source, spawn services,
send network requests or issue a real Modbus/EVCC command. A mutant is killed
when a safety assertion that passes for the real function rejects its result.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import inspect
from pathlib import Path
import sys
import textwrap
import types
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "services"))
from fakes import install_pymodbus_stub

install_pymodbus_stub()
import modbus_broker as broker  # noqa: E402
import watchdog  # noqa: E402
import evcc_manual_lock as manual  # noqa: E402
import energy_priority_policy as energy  # noqa: E402


def compile_guard(function, expression: str, *, replace: bool):
    parsed = ast.parse(textwrap.dedent(inspect.getsource(function)))
    target = [
        node for node in ast.walk(parsed)
        if isinstance(node, ast.If) and ast.unparse(node.test) == expression
    ]
    if len(target) != 1:
        raise AssertionError("source guard location ambiguous or missing: " + expression)
    if replace:
        target[0].test = ast.Constant(False)
    ast.fix_missing_locations(parsed)
    namespace = dict(function.__globals__)
    exec(compile(parsed, "<offline-mutation>", "exec"), namespace)
    return namespace[function.__name__]


class MutationGateTests(unittest.TestCase):
    def test_broker_runtime_safety_role_bypass_is_killed_before_transport(self):
        expression = "operation not in allowed"
        original = compile_guard(broker.Broker.submit, expression, replace=False)
        mutant = compile_guard(broker.Broker.submit, expression, replace=True)

        def forbid_real_transport():
            raise RuntimeError("offline mutation transport deliberately disabled")

        fake = broker.Broker(forbid_real_transport, runtime_mode="control",
                             armed=True, transactions_enabled=True)
        self.addCleanup(fake.close)

        def independent_permission_guard(fn, request_id):
            try:
                fn(fake, "safety_auto", role="safety", request_id=request_id, timeout_s=0.1)
            except PermissionError:
                return
            except Exception as exc:
                raise AssertionError("unprivileged safety operation reached executor") from exc
            raise AssertionError("unprivileged safety operation was accepted")

        independent_permission_guard(original, "baseline")
        with self.assertRaisesRegex(AssertionError, "reached executor|was accepted"):
            independent_permission_guard(mutant, "mutated")

    def test_watchdog_missing_heartbeat_short_circuit_mutant_is_killed(self):
        expression = "age is None"
        original = compile_guard(watchdog.main, expression, replace=False)
        mutant = compile_guard(watchdog.main, expression, replace=True)
        calls = []
        for fn in (original, mutant):
            ns = fn.__globals__
            ns["heartbeat_age_s"] = lambda now: None
            ns["load_state"] = lambda: {}
            ns["attempt_revert"] = lambda: calls.append("unsafe_revert")
            ns["save_state"] = lambda state: calls.append("unsafe_save")
            ns["notify"] = types.SimpleNamespace(send=lambda *a, **k: calls.append("unsafe_alert"))
        self.assertEqual(original(), 0)
        self.assertEqual(calls, [])
        with self.assertRaises(TypeError):
            mutant()
        self.assertEqual(calls, [])

    def test_durable_ev_pause_owner_mutant_cannot_pass_independent_veto(self):
        expression = "record is not None and record['state'] not in ('released', 'revoked')"
        original = compile_guard(manual._assert_no_active_price_pause, expression, replace=False)
        mutant = compile_guard(manual._assert_no_active_price_pause, expression, replace=True)
        evidence = types.SimpleNamespace(read_owner=lambda path: {"state": "owned"},
                                         OwnershipError=ValueError)
        for function in (original, mutant):
            function.__globals__["ownership"] = evidence
        with self.assertRaises(manual.ManualWriterBlocked):
            original(Path("/synthetic/no-live-owner"))
        with self.assertRaises(AssertionError):
            try:
                mutant(Path("/synthetic/no-live-owner"))
            except manual.ManualWriterBlocked:
                pass
            else:
                raise AssertionError("mutant resumed charging under owned pause")

    def test_recovery_priority_mutant_is_killed_without_physical_state(self):
        original = compile_guard(energy.resolve, "recovery", replace=False)
        mutant = compile_guard(energy.resolve, "recovery", replace=True)
        fixture = dict(now=datetime(2026, 11, 7, 9, tzinfo=timezone.utc),
                       readings=[], state=energy.SolarState(), soc_pct=13,
                       reserve_pct=20, save_w=50, recovery=True,
                       ev_charging=False)

        def independent_recovery_guard(fn):
            result = fn("discharge", 650, **fixture)
            self.assertEqual(result.winner, "soc_recovery")
            self.assertEqual((result.action, result.power_w), ("discharge", 650))

        independent_recovery_guard(original)
        with self.assertRaises(AssertionError):
            independent_recovery_guard(mutant)


if __name__ == "__main__":
    unittest.main()
