# Permanent nätprioritering för XC40 på L3 – releaseförberedelse 2026-10-29

**Status: versionshanterad och testad, INTE installerad eller fysiskt aktiverad.**
Bygger på det verifierade provet 2026-10-28:
[runbook](ev-grid-priority-trial-2026-10-28.md) och
[Notion](https://app.notion.com/p/400aa897399b81af9d11f18187955e75).

## Avgränsning och befintlig drift

- Den vanliga `solinteg-dispatch.service` är **ARMED**. Ingen tjänst,
  inverterregister, EVCC/Volvo-inställning, gate eller watchdog har ändrats
  i drift i denna förberedelse.
- Dell kör fortfarande äldre `/opt/solinteg/app/scripts/services/dispatch_loop.py`
  (SHA256 `a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`);
  den är identisk med original-backupen från provet och saknar EV-hooken.
- Den äldre trial-timern är `disabled/inactive`, gate-filen saknas,
  och trialens rollback-status är `restored_original_dispatch`/`rollback_ok=true`.
- **Aktuell blockerare 2026-10-29 18:53 CET:** EVCC-observatören rapporterade
  `read_ok=false, state=unknown` trots färsk tidsstämpel. Inget aktivt
  EV-grid-priority-läge får införas förrän verkliga C2/Ict2/6 A-värden är stabila.
- Git-branchen `Sundsvall` är betydligt nyare än aktiv dispatcher (F-11).
  **Installera inte hela Git HEAD över ARMED-drift** utan separat
  kompatibilitetsgranskning och riskbedömning. Endast det granskade skyddet
  får överföras som minimal versionerad förändring i releasefönstret.

## Versionerad design (ingen extra fysisk aktivering)

- `scripts/services/ev_grid_priority_guard.py` behåller oförändrat schema-1
  för det tidsbegränsade testet och stöder schema-2 för permanent drift.
  Utan giltig gate returneras ursprungligt dispatchbeslut.
- Den nya gate-filen är `/var/lib/solinteg-ev-grid-priority/gate.json`.
  Hela katalogen **och filen** måste ägas av root och inte vara skrivbar
  för grupp eller övriga. Den bor **inte** i det service-skrivbara
  `/opt/solinteg`. En giltig gate kräver exakt policy-ID, unikt
  `activation_id`, `activated_utc` och SHA256 av installerad dispatcher.
  Den medföljande JSON-mallen innehåller avsiktligt ogiltiga platshållare.
- Gate `mode=shadow` beräknar bara förslag. `mode=active` kräver dessutom
  `status.state=active_monitor_ok`, samma `activation_id`, max 40 sekunders
  statusålder, root-ägd watchdogstatus och oförändrat dispatcher-SHA256.
  Saknas något används **ursprungligt dispatchbeslut**.
- Den **separata root/systemd-watchdoggen**, `ev_grid_priority_watchdog.py`,
  ska köras från root-ägd, ej gruppskrivbar
  `/usr/local/libexec/solinteg-ev-grid-priority/`, med en separat
  root-ägd kopia av `ev_grid_priority_guard.py`. Root-watchdoggen
  verifierar att den installerade dispatch-policyn har exakt samma SHA256
  som denna betrodda kopia, och kontrollerar sedan varje 15 s:
  dispatch uppe, checksumma, root-backup, färsk EVCC
  och inverterdata, C2/Ict2, BMS SoC/temperatur, 50006=0, trefas-CT,
  predikterad fasimport, samt att verklig urladdning när EV laddar har
  sjunkit inom rimlig tid (100 s per ny laddsession).
- Fel i datakvalitet, kommunikation, fasbalans, temperatur eller faktisk
  batterirespons gör att watchdog **först återkallar root-gaten**, sedan
  stoppar dispatcher via ordinarie shutdown/Auto, återställer för-release
  dispatcher från root/0400-backup atomiskt och startar tjänsten. Vid
  oväntat nytt fil-SHA vägrar den skriva över okänd kod; status kräver
  då manuell åtgärd. Ingen direkt registerskrivning i watchdoggen.
- En uttryckligen verifierad parkerad/frånkopplad bil med högst 0,6 A,
  även C2 utan faktisk laddström, betyder **ingen prioritering**, inte
  ett permanent fel. Om PV över 100 W eller SoC utanför 25–95 % ger
  policyn endast **standby**, och bara om övriga observationer är
  konsistenta. Övrig SoC-recovery, soloptimering, laddning och idle påverkas inte.
- Vid aktivt EV-läge och inom verifierade nattgränser begränsas bara
  `action=discharge`. 6 A på Ict2, balanserad inverter 50006=0 och
  estimerad icke-EV-huslast på L3 avgör nytt börvärde.
  Beräknad **import på var och en av L1/L2/L3 ≤ 3 200 W**, total
  import ≤ 6 500 W. Detta är försiktiga programvarugränser
  **inte certifierat RMS-fassäkringsskydd**. Ordinarie 20 A-säkringar,
  inverter-/BMS-begränsningar, SoC-golv och
  `l3_charge_guard` förblir självständiga. EVCC-strömtak 6 A, Volvo-mål
  80 %, bilens API:er och FC10 ändras inte.

**Begränsad täckning:** Nattlig 6 A-drift är provad. Att korrekt allokera
solöverskott, högre EV-ström och samtidiga större L1/L2-laster är
**inte fysiskt verifierat**. Under hög PV väljer den nya funktionen
avsiktligt standby framför att påverka övrig styrning; permanent betyder
alltid tillgänglig bevakning, **inte garanterad nätmatning i varje solscenario**.
Ytterligare fysiska prov kräver särskilt tillstånd.

## Checksummebunden minimal dispatcherpatch (endast offline)

F-11 löses genom `scripts/tools/prepare_ev_grid_priority_dispatch.py`, som
läser den verifierade äldre dispatchern och bygger en AST-validerad
kandidat **utan att skriva till /opt eller påverka tjänsten**:

```bash
sudo python3 scripts/tools/prepare_ev_grid_priority_dispatch.py \
  --source /opt/solinteg/app/scripts/services/dispatch_loop.py
```

Detta läsprov på Dell gav original-SHA256
`a2f3aba4be8d64771ed2b20c71c9ceed5c3ce25ff99cad246bc5529afd65a8b7`
och kandidat-SHA256
`7934dd1c31e6ea07780f33492ae57ca33447b3ff7e43259edebd2116ae2db6d4`,
`written=false`. Scriptet avvisar okänd originalchecksumma, ändrade hookankare
och dubbel injektion. Det lägger enbart till `Path`-import och EV-prioritering
före den gamla charge-guarden. Installations- och restartsteg lämnas avsiktligt
ogenomförda tills separat uttryckligt godkännande ges.

## Driftsättningsgrind – får inte utföras utan nytt uttryckligt godkännande

1. Kontrollera drift och Git-version en gång till. Hantera F-11 genom
   minimal och separat granskad patch/hook i *faktiskt installerad* dispatcher,
   inte en okontrollerad kopiering av Git HEAD. Kör offline-tester och
   `python3 -m py_compile` för exakt releasefil; säkerställ att service-ARMED,
   BMS-/SOC- och ordinarie L3-charge-guard-väg förblir oförändrade.
2. Säkerställ EVCC `read_ok=true`, `state=C2`, Ict2 ca 5–6,5 A,
   PWM 100, färska inverter-/fasavläsningar, batteritemperatur,
   fysiska BMS-limiter, huvudsäkringar, 50006=0 och stabil Modbus.
   Stopp om någon förutsättning fallerar.
3. Installera separat rootägd state-katalog
   `/var/lib/solinteg-ev-grid-priority` (0755) och **innan
   kodändringen** en byteexakt root/0400-backup
   `dispatch-before.py` av den exekverare som faktiskt kör.
   Dokumentera båda SHA256; dubbelkontrollera stop/start- och Auto-retur.
4. Installera endast granskade nya policymodulen, watchdog-scriptet
   och den minimala dispatcher-hooken. Watchdog-scriptet och en **identisk
   kopia** av policyn installeras under
   `/usr/local/libexec/solinteg-ev-grid-priority`, root:root och
   utan grupp-/övrig skrivrätt (t.ex. filer 0644, katalog 0755).
   Dispatcherns policymodul ligger kvar i tjänstekatalogen;
   SHA256 mellan båda policymodulerna ska vara byteidentiskt.
   Kör sedan gated **shadow-only**-start,
   med root/0644 `gate.json` schema-2 och `mode=shadow`,
   unik aktivering och checksumma av exakt installerad kod.
   **Även tjänsteomstart i ARMED-installationen måste riskbedömas**
   och utförs först efter godkänt underhållsfönster.
5. Installera
   `deploy/solinteg-ev-grid-priority-watchdog.service` + `.timer`;
   verifiera systemd-konfiguration, rättigheter, JSON-källor och
   att timer kör var 15:e sekund **innan** något aktivt läge.
   I shadow-läge får ingen battery/EVCC-åtgärd ske.
6. Efter *nytt uttryckligt godkännande av fysisk inverteraktivering*:
   byt root-gaten atomiskt till `mode=active` och nytt
   `activation_id`/`activated_utc`, låt watchdog etablera färsk
   `active_monitor_ok` och kontrollera därefter dispatch-protokoll
   (`ev_grid_priority_status`, nytt börvärde), reell batteriurladdning,
   nätimport per fas, BMS- och säkringsmarginal. Börja med 6 A L3
   och jämför mot 28 oktober. Avbryt vid avvikelse.
7. Dokumentera ny driftstatus, tidsserier, återställningsresultat,
   exakt release-SHA256, testresultat och beslut i Notion och Git.
   Frisläpp först efter godkänd mätning över relevant belastningsvariation.

## Rollback och oberoende skydd

- Automatisk watchdog: återkalla gate **före** dispatcher-stop,
  stoppa ARMED-tjänsten på ordinarie sätt så att styrningen återgår
  till Auto, återställ backup atomiskt, starta om och kontrollera aktiv
  tjänst samt SHA. Status
  `restored_previous_dispatch` + `rollback_ok=true` betyder framgång.
- Vid `rollback_failed_*`: root-gate ska vara återkallad; **inget
  blint återförsök att skriva över annan dispatcher**. Stoppa och
  felsök med operatör.
- Manuell återställning följer samma ordning. Watchdog-timern får
  inaktiveras **först efter** kontrollerad rollback.
- Den vanliga `solinteg-watchdog.timer` och BMS-skydd ska vara aktiva
  hela tiden. Återgång till tidigare dispatch kan återintroducera
  husbatteriurladdning under EV-laddning; rollback återställer beprövad
  fysisk säkerhet, **inte nätprioriteringens ekonomi**.

## Kvalitet och avgränsat leveransbeslut

- Offline: befintliga 16 trialpolicyfall/9 trialwatchdogfall
  kompletterade med produktionsgate, statusålder, root-kontroll,
  SHA-lås, L1/L2/L3-import, standby och byteexakt rollback.
- `systemd-analyze verify` ska godkänna de nya enheterna.
- `python3 -m unittest discover -s scripts/tests -p 'test_*.py'`,
  `npm test`, `npm run lint`, `npm run typecheck` ska passera.
- Denna ändring får committas på lokal `Sundsvall`, men inte
  aktiveras/deployas förrän driftgrindarna ovan är uppfyllda och
  uttryckligt tillstånd har inhämtats. Push av äldre lokala commits
  kräver separat granskning av privat konfiguration.
