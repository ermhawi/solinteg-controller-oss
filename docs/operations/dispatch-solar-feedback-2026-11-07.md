# D06 · Policyhysteres, registerkvittens och efterföljande solfeedback

**2026-11-07 · `tested/source`, ingen driftsättning.** Utgångspunkten var en reproducerad lucka i `dispatch_loop.main`: även om `needs_apply()` returnerade **False** kunde `next_energy_priority_state.capturing=True` lagras utan nytt registerkommando eller efterföljande observation. Ett `applied`-resultat kunde dessutom vara en accepterad setpoint, inte verifierad laddning. Den äldre installerade site-dispatchern är en separat fil och är **inte** påverkad av denna källändring.

## Liten, separerad återkoppling

`scripts/services/dispatch_solar_feedback.py` innehåller två rena funktioner. `observe_post_apply_general` kräver en tidigare ackad idle/General-begäran, **två olika och kronologiskt färska FC03/poller-sampel efter begärans UTC-tid** (15–55 s mellan), verifierad effektbalans, och `work_mode_raw=0x101` på **båda** senaste mätningarna. En nyare ogiltig, felaktig, för gammal eller EMS-mätning är **inte** ett godtagbart kvitto. Observationerna klassas explicit som:

- `unknown`: ingen styrkvitto-bunden, färsk och oberoende General-evidens.
- `observed_general`: General-läge läst från pollern, men **ingen verifierad batteriladdning**. Detta behåller den befintliga hysteresislatchen vid tillfälligt husunderskott; faktisk batteri-W får fortsatt avsluta via `energy_priority_policy.resolve`:s 30 Wh/urgent-budget.
- `observed_charging`: General-läge och minst 100 W faktiskt uppmätt batteriladdning i båda sampel. Detta är **inte** verifierat firmware-/BMS-/EndSOC-skydd.

`reconcile_solar_state` tar den **föreslagna**, aldrig förhandskvitterade, `SolarState` och ett explicit utfall. Den återlämnar `capturing=True` **endast** om målhandlingen är `idle`, tidigare ackad General-begäran finns och den efterföljande observationen är `observed_general` eller `observed_charging`. Annars är det bara *policyintention*, aldrig påstådd fysisk aktivering.

| Styrning/återkoppling | State efter beslut | Fysisk klassning |
| --- | --- | --- |
| `applied` men ingen oberoende ny mätning efter ACK | Reset `capturing=False` | `unknown`, även om registerskrivningen lyckades |
| `needs_apply=False`, tidigare bekräftat General och två färska post-ACK mätningar | Behåll föreslagen hysteresislatch | `observed_general` eller `observed_charging` |
| `needs_apply=False`, ingen tidigare ACK eller saknad/korrupt sampel | Reset | `unknown` |
| `accepted_pending_physical_confirmation` | Reset | `unknown` |
| `skipped_divergence` / `skipped_solar_shortfall` | Reset | `unknown` |
| `inhibited_broker_telemetry` och `error_reverted` / `error_revert_failed` | Reset | `unknown`, ingen blind retry från feedbackgrinden |
| Ekonomisk EMS-save och cooldown | `capturing=False`, bevara explicit `blocked_until` när policyn begär det | `unknown` |
| Ett nytt tick efter oväntat exceptionsblock eller återgång till `decide()` | Reset och rensa tidigare `solar_idle_ack_at` | `unknown` |

Källintegration i `dispatch_loop.py` är bara aktiv när befintlig `DISPATCH_ENERGY_PRIORITY=1` är vald; **standard är fortfarande `0`**. `solar_idle_ack_at` sätts efter accepterat faktiskt styrförsök **endast** om `ARMED`, target är idle och loop inte redan var i Auto. Varje misstag, inhibering, ny icke-idle-action eller felstreak tar bort ack. Feedbackläsning är **read-only** från befintlig SQLite; dess fel loggas och får **inte** initiera `revert_best_effort` eller separat Modbus-klient. `control_actions` kan enbart bära `energy_priority_physical_confirmation` när gammal ACK och föregående observation fanns **före** nytt skrivförsök; kandidatfältet `energy_priority_solar_state` märks uttryckligen `energy_priority_solar_state_quality=proposed_not_physical`.

**Verifiering:** `scripts/tests/test_dispatch_solar_feedback.py`: 9/9 isolerade matristester, inklusive ett rött test för inkonsekvent discharge-kandidat före fix och faktiskt observerat `General` under kort underskott utan falskt `charging`. Dessutom 20/20 D03-integrationsfall och befintliga dispatcherregressioner. I denna iteration körs hela regressionssviten från **staged Git-index**, inte endast från 13 äldre lokalt ocommittade filer. Status `source/tested` först efter grön full svit.

**Kvarvarande NO-GO:** Även två färska pollermätningar är inte ett tillverkarverifierat BMS-skydd eller ett bevis att byte till General inte dränerar batteriet när moln kommer. Fysiska D08/D09/V kräver tidsstämplat replay, verifierad installerad dispatcher, ström/CT-kvalitet och separat uttryckligt operatörs-GO. Inga `/opt`-filer, systemd-units, produktionsflaggor eller Modbus/EVCC skrivningar har ändrats.
