# D03 · Kolliderande energibeslut: ren policy kontra installerad dispatcher

**2026-11-07, status `tested` för ren policy, D03 fortsatt öppen för dispatcherintegration.** Den isolerade policyn `scripts/services/energy_priority_policy.py` är byteidentisk med den installerade modulen enligt R01; dess 8 riktade fall samt 80 deterministiska repetitioner verifieras i `scripts/tests/test_energy_priority_conflicts.py`. Ingen fysisk input/output förekommer.

| Situation/Given | When | Then / observerad policyvinnare | Risk som återstår i full dispatcher |
| --- | --- | --- | --- |
| SoC 13 %, `recovery=True`, plan discharge | Policyresolver körs | `soc_recovery`, behåll recovery-beslut | Dispatcherns tidigare recovery-funktion kan ändra effekt, datakvaliteten måste bevakas |
| Plan `charge=1500W`, solöverskott stabilt | Resolver körs | `planned_charge` (ändrar inte laddningen) | Senare L3-grind sänker eventuell effekt; saknad färsk L3 kan inhibera |
| EV-status okänd, plan discharge | Resolver körs | `unverified_ev`, behåll plan | Bevarad discharge är **inte** bevis att bil är frånkopplad; full dispatcher måste övervaka L3 |
| EV laddar, plan discharge | Resolver körs | `ev_grid_first` ⇒ `idle,0` | Source-only operatörsinitiativ för bilens manuella paus får aldrig överköras |
| Stabil PV-export, SoC 16 % under 20 %-reserv | Resolver körs | `solar_capture` ⇒ `idle,0` | `idle` kan ge fysisk Auto som inte alltid laddar batteriet; endast source-status |
| Samma export vid SoC 50 % | Resolver körs | `no_override`, plan discharge | Prisplanering kan fortsätta att prioritera urladdning även när elpris är lågt |
| Ingen telemetri / otillräcklig PV | Resolver körs | `solar_unverified` eller `no_override`; ingen påhittad solfångst | **Fails-open mot plan**: source-only beteende, behöver D04/S08 explicit kvalitetsgrind före drift |
| Batterispar 50 W + stabil sol | Resolver körs | `solar_capture` ⇒ `idle` | Förhindrar vissa men inte alla cykler; `hold` har separat default-off fysisk begränsning |

Samtliga förväntade vinnare testas utan externa anrop. Det direkta `resolve`-resultatet är stabilt under identiska signaler i 80 iterationer, vilket **inte** bevisar hysteres vid brus/SoC-gräns. Full systemföljd och överstyrningspunkter finns i [D01](dispatcher-installed-priority-2026-11-07.md), och installerad dispatcher har 13 semantiska skillnader mot källan ([R03](dispatcher-source-runtime-diff-2026-11-07.md)).

**Öppna D03-grindar:** prissteg/spotpersistens, full EV-/L3-lastbalansering (speciellt unknown EV och fas 20 A), blandad battery-save och `reserve_hold`, bortfall av Modbus-readback, planens `solar_skip/expected_soc` genom alla 13 steg och fysiskt avsiktlig General-övergång. Skapa fryst installerad golden master och komplettera mockad integration innan D03 kan bockas av. Inga produktionsregler ändras här.