# Python service tests

Unit tests for the pure decision logic in `scripts/services/` — slot indexing, SoC-drift
interpolation and the apply half (`dispatch_loop.py`), Modbus write ordering plus the fast-path
optimization (`inverter_control.py`), and the independent watchdog fail-safe that forces the
inverter back to auto if the dispatch loop's heartbeat goes stale (`watchdog.py`).

Deliberately stdlib-only (`unittest`), matching this project's own "no extra dependencies"
convention for the services themselves — no pytest, no test-only pip installs needed on a
fresh dev machine or the deployment box.

`fakes.py` stubs `pymodbus` (a deployment-only runtime dependency, not expected to be
installed in a plain dev environment — `inverter_control.py` hard-exits at import if it's
missing) so `inverter_control.py`/`dispatch_loop.py`/`watchdog.py` can be imported and
exercised against an in-memory fake Modbus client instead.

**What's NOT covered here, deliberately:** `decide()` in dispatch_loop.py (needs a real
telemetry.db, or a much larger fixture harness) and `main()`'s signal-handling/sleep loop —
verify that side against your deployment's own `control_actions` rows instead.

That carve-out used to swallow more than it should have. `main()` also held the whole APPLY
half inline — guard evaluation, the register write, and two levels of failure recovery — so
"main() is plumbing" quietly excused ~165 lines of real safety logic from any test. It is now
`effective_target` / `needs_apply` / `check_soc_divergence` / `apply_decision`, and covered:
the write-gating rules (including that an idle target is never re-asserted on a timer, which is
what stops the loop stomping a work mode you set by hand in the inverter's own app), and both
failure branches — that a telemetry write error cannot skip the fail-safe revert, and that
`loop_in_auto` goes False when the revert itself also fails.

## The one `scripts/tools/` test

`test_fetch_ellevio_history.py` is the exception to "services only". Offline tools are
otherwise left untested on purpose — they're run by hand, and a wrong answer shows up in the
output you're already reading. This one doesn't: it covers the guard that catches Ellevio
answering for the *other* meter when `--site` and `--direction` disagree, which the API does
with HTTP 200 and a full slot count. Nothing about that failure is visible downstream — the
files are named correctly and full of plausible numbers — so the guard is the only thing
standing between it and a `reconcile-ellevio-meter.py` run that compares import against itself
and declares the export side perfect. A guard against a silent failure has to be tested, or
it's just a second silent failure. The dashed filename means it's loaded via `importlib.util`,
not a plain import.

## Offline Modbus-broker regression

`test_modbus_broker.py` covers the explicitly **non-deployed** single-owner
broker prototype and a one-connection fake Modbus dongle (never real TCP:502).
It tests Unix-domain-socket credential roles, request-id idempotence,
five-stage FC06 faults/Auto rollback, queued safety preemption, cloud stalls,
mode flips and rejection of the contradictory SoC25 sample. Current
`modbus_poller.py`/`dispatch_loop.py`/`watchdog.py` are unchanged;
see [baseline and limitations](../../docs/modbus-broker-offline-2026-11-01.md).

`test_modbus_broker_readonly.py` additionally verifies the opt-in
stage 3 transport and poller compatibility **without inverter connections**:
strict FC03 allowlist/slave ID, all FC06 refused, startup arming gates,
read-only IPC and original `live.json`/SQLite schema, plus deliberate
broker/rejection failure without opening the direct TCP fallback.
The service templates are **not installed**; the stage 3
[cutover blockers](../../docs/modbus-broker-readonly-stage3-2026-11-02.md)
must be resolved before any physical migration.

`wire_smoke_readonly.py` is an **explicit standalone real-pymodbus
integration smoke** (not included in default unittest discovery).
Run it only with an environment containing pymodbus>=3.13; it uses
a synthetic loopback 127.0.0.1 Modbus-TCP fake and verifies 12 FC03,
zero FC06, slave 255 and broker read-only enforcement. No inverter is
contacted.

## Running

```
python3 -m unittest discover -s scripts/tests -p "test_*.py"
```

(on Windows, use the `py` launcher in place of `python3` — same as `npm run test:py`). Run a
single module or add `-v` for verbose per-test output as usual.
