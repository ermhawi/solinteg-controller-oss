# D04 · Immutabelt, fysiskt osäkert beslutsbevis (source-only)

**2026-11-07 – `tested/source`; inte inkopplat i installerad dispatcher.** `scripts/services/dispatch_decision_contract.py` definierar en **frusen** dataclass `DispatchDecision`, en minsta kompatibilitetsadapter `from_priority(PriorityDecision)` samt deterministiska, sanerade loggfält `log_fields(observed_at=UTC)`. Den läser inga filer och kan inte exekvera någon styrning, Modbus eller EVCC.

| Fält | Kontrakt |
| --- | --- |
| `action`, `power_w` | De föreslagna värdena från en redan beräknad arbitration. Effekt är en icke-negativ, begränsad integer W; bool, negativt och överskriden gräns avvisas |
| `origin`, `winner`, `reason` | Policyägare, vinnande regel och läsbar orsak; origin/winner begränsas till säkra identifikatorer, textorsak har längdgräns |
| `data_quality` | Exakt `fresh`/`stale`/`unknown` för beslutets underlag, **inte** en fysisk säkerhetsnivå |
| `guard_results` | Immutabel tuple av identifierade SoC/EV/L3/broker-villkor. Att ett granskningstest finns betyder inte att ett fysiskt stopp är installerat |
| `requires_register_readback` | Explicit bool för förväntad senare FC03/readback; innehåller **inget** kvitto på genomförd skrivning |
| `expected_soc_kwh`, `solar_skip` | Prognos- och PV-finansieringsmetadata i samma kontrakt; kWh valideras som ändligt och icke-negativt |
| `physical_confirmation` | **Alltid `unknown`**. Om någon försöker ange `verified` avvisas objektet innan det kan loggas |

`log_fields` ger `schema=solinteg-dispatch-decision-v1`, UTC-tidszon, samma action/W/winner/reason, guard-/kvalitetsmetadata och explicit `decision_state=proposed_unverified` + `physical_confirmation=unknown`. Funktionen är för tester och en **framtida** loggadapter i D05: det är *inte* en ny `control_actions.applied`-rad, ingen systemd- eller dispatcherändring och inga nya tjänster. De gamla aktuella returvärdena från `energy_priority_policy.resolve` lämnas helt oförändrade.

**Regression:** `scripts/tests/test_dispatch_decision_contract.py` **5/5 PASS**: input/output-paritet för `action/power_w/winner/reason` mot verklig ren policy, JSON-kompatibelt ofysiskt loggobjekt, mutabilitetsspärr, 13 felaktiga typ-/värdevarianter inklusive `NaN` och otillåtet `verified`, timezonekrav och identisk upprepad serialisering utan state-reset.

**Nästa koppling, ej redan utförd:** D05 måste göra en liten kompatibilitetsadapter på en isolerad policygren i **den site-patchade** dispatchern och jämföra med golden-master/replay innan den får ersätta något aktivt beslut. D06/D07 måste dokumentera skip/inhibited/reassert och readback som separata tillstånd; ett fysiskt `General` eller ett `accepted_pending_physical_confirmation` får **aldrig** tolkas som `physically_verified`. Se [D01:s installerade prioriteringsordning](../dispatcher-installed-priority-2026-11-07.md), [T02:s fem nivåer](../operations/control-evidence-2026-11-07.md) och [S05:s fencing](adr-2026-11-07-modbus-fencing.md).
