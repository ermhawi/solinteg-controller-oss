# ADR 2026-11-02: stale brokertelemetri och SoC-medveten dispatch

**Status:** klassificerare och opt-in-integration i dispatcher verifierade **endast offline** (2026-11-03).
**Live-cutover:** **NO-GO**. Ingen installerad dispatcher, watchdog, broker eller systemd-enhet har ändrats.

## Bakgrund

Den aktiva `dispatch_loop.py` avvisar `live.json` när tidsstämpeln är äldre
än 120 sekunder, men `live_discharge_power_w` och `live_charge_power_w`
kan då behålla en effekt från den tidigare planen. Vid okänd SoC kan
`prioritize_soc_recovery` öppna en egen Modbusförbindelse för en direkt
läsning. Det är inte förenligt med framtida exklusivt brokerägarskap.
Att automatiskt välja General/Auto kan samtidigt förvärra låg-SoC-läget.

## Föreslaget kontrakt

`scripts/services/modbus_broker_stale_policy.py` är en fristående, ren
beslutsfunktion för **framtida** koppling. Den importeras inte av den
driftsatta dispatchern. Funktionsresultatet saknar register, effektbörvärde
och kommando: en kandidat är **inte** fysisk skrivbehörighet.

| Observation | Offlineklassificering | Operativ innebörd efter separat godkänd integration |
| --- | --- | --- |
| Färskt, godkänt brokersample och SoC över återhämtningsgränsen | `plan_candidate` | Normal plan får endast granskas vidare av befintliga SoC-, fas-, BMS-, arming- och readbackskydd |
| Färskt, godkänt brokersample med låg SoC eller aktiv hysteres under exit | `recovery_candidate` | Säkerhetsåterhämtning måste granskas; planen får inte vinna över den |
| Ej godkänt/stale brokersample, men oberoende, färsk och verifierad låg SoC | `recovery_candidate` | Endast säkerhetsförslag; privilegierad väg, exklusiv TCP och fysisk verifiering krävs fortfarande |
| Stale/ogiltig telemetri och oberoende hög SoC | `inhibit` | Ingen ekonomisk planstyrning utifrån stale PV/last |
| Okänd eller stale SoC, saknat/felaktigt samples, saknad tillit eller ogiltig konfiguration | `inhibit` | Ingen ny planerad fysisk effekt, ingen implicit Auto; begär oberoende säkerhetsbedömning |

### Kontroll av mätunderlag (offlineförslag)

- Acceptera endast `sample_quality=valid` med `sample_id` från den
  framtida brokerpollern. Äldre direktpollers `live.json`, utan sådan
  kvalitetsmärkning, är **inte** godkänt underlag för denna nya grind.
- Läs tidszonsmedveten tidsstämpel med ålder **0–120 s**, inte framtida;
  samplingsduration **0–20 s**. Gränserna är prototypvärden och kräver
  kalibrering mot den fysiska dongeln innan driftsättning.
- SoC måste vara ändligt tal inom 0–100 %, PV och huslast ändliga och
  icke-negativa, och alla CT-faser och nätvärdet ändliga och konsistenta
  enligt brokerns nuvarande tolerans. Driftläge måste vara General
  (`0x101`) eller EMS BattCtrl (`0x303`). Annan operatörsvald eller
  okänd mode får inte implicit godkänna planstyrning.
- Återhämtningsgränser tas som indata från de befintliga
  `SOC_RECOVERY_ENTRY_PCT`/`SOC_RECOVERY_EXIT_PCT`. Hysteresen
  gäller fortfarande vid aktiv återhämtning, även över entry.
- Oberoende `IndependentSoc` kan endast ge en
  **återhämtningskandidat**, aldrig godkänna en ekonomisk plan när
  PV/last är stale. Flaggan `verified` i offlinemodellen är en
  injicerad testförutsättning; den är **inte** en driftsatt verifiering
  av identitet, datakälla eller säker TCP-åtkomst.

### Viktig säkerhetsbegränsning

`inhibit` är **inte** ekvivalent med `idle` eller `return_to_auto`.
En senast bekräftad aktiv urladdning kan behöva avbrytas. Att bara
underlåta nya skrivningar kan annars lämna ett gammalt tvångsbörvärde
aktivt; att blint gå till Auto kan å andra sidan fortsätta urladda ett
lågt batteri. En oberoende, privilegierad supervisor måste därför
hantera okänt fysiskt läge, larm, timeout, verifierad exklusiv
anslutning, säker övergång och readback. Modellen **löser inte**
detta utan markerar behovet av en sådan säkerhetsväg.

## Opt-in-koppling i dispatchern (källkod 2026-11-03)

`DISPATCH_BROKER_STALE_GUARD=1` kopplar nu in samma klassificerare i
`dispatch_loop.py`, **avstängd som standard** och explicit spärrad när
`SOLINTEG_CONTROL_ARMED=1`. Denna kod är avsedd för isolerade tester
tills oberoende supervisor, säker övergång och ensam Modbus-skrivare finns.

- Före `decide()` kontrolleras brokersample, inklusive giltig tidsstämpel,
  kvalitetsstatus, SoC/hysteres och CT-/modekonsistens. Inget ekonomiskt
  beslut och ingen direkt SoC-fallback körs när kontrollen misslyckas.
- `sample_id` och tidsstämpel måste gå monotont framåt; identiskt färskt
  sample kan återanvändas. En annan mätning under beräkningen innebär att
  börvärdet måste beräknas om, inte appliceras med gammalt underlag.
- Samma mätning verifieras igen efter beslutsberäkningen, vid ingången
  till `apply_decision()` och omedelbart före den befintliga skrivvägen.
  Misslyckat test resulterar i `inhibited_broker_telemetry`, **utan nya
  registerskrivningar** eller implicit General/Auto-fallback. I detta
  offlineprototypsteg kan en läsning från en tidigare godkänd fas redan ha
  öppnat en Modbusklient, men ingen fysisk styrning är armerad.
- `recovery_candidate` är ännu **inte** tillstånd att ladda; även färsk
  låg SoC spärrar automatiska åtgärder i brokerprototypen. Spärrat läge
  kvarstår under processens livstid. `dispatch-heartbeat.json` behåller
  tidsstämpeln men får även `broker_policy=inhibit` och
  `supervisor_required=true`. Dagens watchdog läser fortfarande
  **endast tidsstämpeln** och reagerar inte på dessa nya fält.
- Vid oväntade dispatchfel i opt-in-läge används ingen blind Auto-fallback;
  vid avslut gäller samma försiktighet. Den gamla direktlägesvägen och
  dess revert/watchdog är helt oförändrade när flaggan saknas.

**Viktig kvarstående säkerhetslucka:** att inhibera nya börvärden kan
lämna ett redan aktivt EMS-börvärde kvar. Latchen är dessutom endast i
processminne och ersätter inte en persistent, oberoende supervisor.
Denna etapp **får inte** aktiveras för skarp styrning; den fristående
watchdoggen, brokerägarskap, fysiskt säkert stopp/readback och
verifierad SoC-källa återstår fortfarande.

## Offlineverifiering

```sh
python3 -B -m unittest discover -s scripts/tests -p test_modbus_broker_stale_policy.py
python3 -B -m unittest discover -s scripts/tests -p test_dispatch_loop.py
```

16 klassificerartestfall täcker tidsgräns/future/naiv tidsstämpel,
kvalitetsstatus, fas-/effektdata, ogiltig SoC, provgränser, underkända
legacy-samples, SoC-entry/exit-hysteres och oberoende SoC-källa.
Därtill 12 nya dispatcherregressionstester 2026-11-03: replay, monotonicitet,
SoC-/CT-spärr, kontroll efter läsning och före write, heartbeat,
fail-closed vid gammal data och beslutsfel, processlatch och armed-spärr.
Alla tester använder fake/inmemory; inga anrop till systemd, fysisk
Modbus, växelriktare eller produktionsfiler. Hela Python-sviten:
**1 292 tester OK**.

**Återstår:** driftsatt oberoende SoC-verifiering, persistent säkerhetslatch,
säker hantering av redan aktiva EMS-börvärden, supervisor med exklusivt
TCP-ägarskap, reell registerläsning/readback och kontrollerad fysisk
rollback. Grinden är endast testad mot fake-klienter, inte fysisk
inverter. **NO-GO kvarstår**.

Relaterat: [etapp 3-grindar](../modbus-broker-readonly-stage3-2026-11-02.md)
och [migrationsplanen](../plans/modbus-broker-2026-11-01.md).
