# Upstream integration (2026-10-19)

Source: hspolander/solinteg-controller-oss `origin/main` at `f44418b`.
Local base: `Sundsvall` at `f399cd5`. Integrated by a merge commit, **not** a rebase.

## What is included

- Grid meter per-phase monitoring from `8135a63`: registers 10994/10996/10998,
  SQLite migration, dead-CT check, tests. The poller validates the phase sum against total
  power and falls back to the existing 11000–11029 block (caching lack of support per
  connection). A read-only probe on the Sundsvall inverter on 2026-10-19 confirmed the 36
  register block works; L1+L2+L3 differed from grid total by 1 W.
- Optional solar-premium survival (`d94f85b`) and deferral-until-needed (`4399b48`).
  Adapted to preserve the local 12 h deferral cap, 12 h solar lookahead, price forecast premium,
  SoC/charge limits, reserve margin and combined risk-sacrifice guard.
- Adapted replay support (`104cfc5`): uses the same risk option switches and
  load reserve when pricing the planner's candidate trajectories.
- Opt-in refund-aware comparison (`f44418b`): credits only grid-origin energy exported
  in forecast no-solar slots; treats initial energy as unknown, consumes grid-origin stock
  for house load first, removes conversion loss, and compares both candidates on the same
  reserve, terminal-value and risk-guard basis. Skatteverket type 739:
  https://www.skatteverket.se/foretag/skatterochavdrag/punktskatter/energiskatter/aterbetalningavskattpaelochbransle.4.109dcbe71721adafd252816.html
- Fixed the candidate-scoring discrepancy: `evaluateDispatch` keeps its historic honest
  forecast valuation by default, and has `useConsumptionReserve: true` for DP candidate
  comparisons, matching the DP's effective load. Hindsight/oracle remains unchanged.
- Both deploy variants restart poller/weather after code replacement (when present locally).

## Explicitly not enabled or copied

- Three upstream household load-shape commits `bc5bc36`, `a3b6426`, `bb982b5`
  are **not** copied. Their measurements are from another site. Sundsvall already has its own
  live trailing profile; the reference static profile is a fallback only.
- Upstream hold `a968405` is **not wired into our dispatch loop**. `force_hold` and probe
  scripts are kept as research tools, but the planner's `hold` remains distinct from the
  executor-only `freeze`. A separate Sundsvall probe of the *three-register full freeze*
  imported about 1.3 kW and charged the battery. Upstream's *soft hold* (50207=0, caps open)
  succeeded on a different device but has not been probed on Sundsvall firmware.
- `SOLINTEG_SOLAR_RISK_SURVIVAL` and `SOLINTEG_DEFERRAL_UNTIL_NEEDED` default OFF until
  local historical replay confirms savings without regressions.
- `SOLINTEG_REFUND_CREDIT` defaults OFF, and `SOLINTEG_REFUND_TAX_PAID_ORE` defaults to 0.
  Only enable after proving refund eligibility, actual regional tax paid, and intent to file
  the annual claim. No 2027 amount is assumed. This is a *planning estimator*, not a claim.

## Verification

- Python `python3 -m unittest discover -s scripts/tests` (includes legacy Modbus fallback tests)
- Vitest `npm test -- --testTimeout=30000`
- `npm run typecheck`, `npm run lint`, `npm run build`
- Read-only Modbus probe extended 10994x36 and legacy 11000x30 on the real inverter
- Verify healthcheck, poller freshness, current optimizer plan and dispatch after deployment

For any further upstream integration, compare actual **semantic behaviour** on this site,
not just absence of Git merge conflicts. Do not use a global `-X ours` or `-X theirs`.
