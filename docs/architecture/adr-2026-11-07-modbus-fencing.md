# ADR 2026-11-07: Modbus-ägarskap och säkerhetsfencing (S05)

**Beslut: NO-GO för ny automatisk fallback eller ny supervisor tills ett verifierat single-owner-övertagande finns.** ADR beslutar principer och ansvar, **inte** live-systemd, broker/EVCC-write eller firmwarefunktion. Berör `modbus_broker_control_service.py`, `modbus_broker.py`, legacy `watchdog.py`, `inverter_control.py` och systemd-enheter. Äldre [broker-fel-ADR](adr-2026-11-04-broker-legacy-failure-scope.md) och [EMS/General-risk](adr-2026-11-02-ems-general-failsafe.md) gäller.

## Ansvar per skede

| Övergång | Enda tillåtna fysiska skrivare | Fencing-/förutsättningsbevis | Ingen säker växling om |
| --- | --- | --- | --- |
| Normalläge, dispatcher begär styrning | Aktiv **brokerprocess** som TCP:502-ägare; dispatcher får endast skicka begränsad broker-IPC | Peer-cgroup + per-operation allowlist + broker-kö + transport-ägarskap | Socketägare eller processens Modbus-ägarskap okänt |
| Normal restart av dispatcher | Befintlig broker kvarstår; ny dispatcher använder samma broker-IPC | Giltig data, oförändrat source-of-truth för target | Ingen blind återutsändning vid förlorat IPC-kvitto |
| Brokeråterstart / extern Wi-Fi-omstart | **Ingen** ny skrivare förrän gammal transport är bevisat stängd, in-flight markerad okänd och en ny ensam broker verifierad | Gammal PID/socket och lease borta, TCP-exklusivitet verifierad; en aktuell FC03-profil som observation | Det går inte att bevisa exklusivt ägarskap; explicit NO-GO |
| Legacy watchdog märker gammal heartbeat | I dagens system finns äldre best-effort `return_to_auto` som kan konkurrera; detta är en **känd brist**, inte nytt godkänt ägarskap | Framtida supervisor måste begära broker-serialiserad `safety_auto`; inte öppna egen socket medan broker äger transport | Broker nere, delvis registersekvens eller okänd SoC/BMS; då NO-GO i denna ADR |
| Framtida skyddad supervisor | Endast **separat godkänd** supervisorcgroup med skyddat servicekonto och säkerhetsroll, **via existerande broker** medan broker äger TCP | Rollmappning, operationgrind, säkerhetslatch, idempotent request-ID och verifierat ingen annan writer | `safety_auto` nekas i dagens kod; rollen distribueras till ingen aktiv cgroup |
| Root/systemd-stop av broker/dispatcher | Högst en pågående säkerhetssekvens hos **verifierad ägare**; aldrig root-probe samtidigt | Stoppsekvens avslutar in-flight och rapporterar unknown; endast markerad operator-återställning via ny enkel ägare | `ExecStopPost` kan inte bli garanterad oberoende om hosten tappar ström |
| Dispatcher/broker helt offline, Dell-avbrott | **Ingen lokal ägare** kan nå inverter; ingen ny fallback antas | Kräver extern oberoende mekanism och tillverkarverifierad EMS-timeout eller fysisk BMS-verkan | Kvarhängande EMS är möjlig; native EndSOC 10 % har ej verifierats stoppa aktivt EMS-börvärde |
| Manuellt håll-/frysprov | Framtida separat kvalificerad broker-`hold_probe` endast när dispatcher är **bevisat disarmed** och inget annat prov äger transport | Root-ägda recovery-markers, fail-safe läsvillkor, operatörens särskilda test-GO | Nuvarande `solinteg-hold-probe.service` är inte installerad; prova inte fysiskt |

## Invarianter och stoppvillkor

1. **En enda faktisk FC06/TCP:502-ägare** vid varje möjlig skrivsekvens. Inbyggd `flock` är endast lokal samordning; extern/handdriven klient måste hanteras via nätverksfencing eller fysisk exklusivitet, inte antas bortkopplad.
2. Ett IPC-timeout efter en skrivning är **unknown** och får inte retrys blint. En ny request-ID ersätter inte verifiering; queue-fencing gäller även in-flight arbete.
3. `safety_auto` är i dagsläget **nekad** i control runtime och har ingen produktionscgroup. Rätta först S06 endast i offlinekod och under disabled-by-default-flagga. **Ingen testdrift innebär automatisk GODKÄND säkerhet.**
4. Återgång till General `0x101` är **inte** en generell säkerhetsnivå om aktuell SoC är under 20 % ekonomisk reserv, eller okänd; BMS/native EndSOC får inte förväxlas med 20 %-skydd. **Ingen blind Auto**.
5. `write_heartbeat`, systemd processstatus, registerreadback eller en `safety_latched`-flagga ensam verifierar aldrig batteriets verkliga effekt; separat `physical_state_unknown` i [S03-kontraktet](adr-2026-11-07-control-health-contract.md) måste observeras oberoende.
6. **Fysiskt stopp:** om ägare/SoC/telemetri/återställningssekvens är okänd, avsluta aktiva nya skrivförsök, bevara incidentlarm och begär manuell verifiering. Utför inte en ny `Auto` i blindo.

## Tillämpning och återställning

Denna ADR godkänner **ingen** installation, ändring i root-/systemd-unit, Wi-Fi-reset, Modbus- eller EVCC-kommandon. Förbered S06 (testad safety-roll och operationgrind, default off), S07 (fake fault injection) och S08 (loopback-leased ownership); fysisk S09/V först efter tillverkardata och separat GO. Vid varje verklig migration: privat manifest av exakt installerade filer, avgränsade atomära filbyten med SHA, test av den frysta kandidaten, read-only rapport före/efter, bevarat rollbackpaket och operatörens stoppsignal. Om någon övergång inte kan garanteras exklusiv = **NO-GO**.

**Evidens 2026-11-07:** [kontrollmatris](failure-mode-matrix-2026-11-07.md), 5/5 reproducerande fake-/watchdogkontraktstester; [manifestet](../operations/release-inventory.md) visar 7 kända käll-/driftavvikelser och `manifest_gate=blocked`. `source`-beslut, inte `installed`.

**S06 uppföljning (commit `428ee63`, `tested/source`):** [Exakt supervisorroll och default-off-fysisk preflight](../operations/supervisor-safety-gate-2026-11-07.md) har 8/8 fake-tester och 13/13 tester från en isolerad staging av endast godkända S06-hunks. Inga nya runtime-units har installerats. Den framtida rollen kan fortfarande inte användas vid oägd EMS, okänd CT eller SoC under 20 %, och fysiskt skydd är fortsatt **NO-GO**. S07/S08 och separat V/GO kvarstår.
