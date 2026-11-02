# ADR 2026-11-02: stale brokertelemetri och SoC-medveten dispatch

**Status:** proposed för driftsättning, klassificerare verifierad **endast offline**.
**Live-cutover:** **NO-GO**. Inga befintliga dispatcher-, watchdog- eller systemd-vägar har ändrats.

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

En senare integration måste verifiera färskhet och brokerägarskap
omedelbart före varje fysisk transaktion, övervaka återanvändning av
`sample_id`/generation, förbjuda ny direkt Modbus-SoC-läsning vid
stale data, samt dokumentera hur redan aktiva tvångsbörvärden stoppas
utan BMS-/SoC-risk. Detta steg får inte ta över den fristående watchdoggen.

## Offlineverifiering

```sh
python3 -B -m unittest discover -s scripts/tests -p test_modbus_broker_stale_policy.py
```

16 testfall täcker tidsgräns/future/naiv tidsstämpel, kvalitetsstatus,
fas-/effektdata, ogiltig SoC, provgränser, underkända legacy-samples,
SoC-entry/exit-hysteres och oberoende SoC-källa. Inga anrop sker mot
systemd, fysisk Modbus, växelriktare eller produktionsfiler.

**Återstår:** utarbeta en säker koppling i en isolerad dispatcher-testhärva,
oberoende verifiering av SoC-källan, färskhets-/replaykontroll vid
användningsögonblicket, säkert avslut av tidigare aktiv tvångsstyrning,
driftsatt safety-supervisor med exklusiv ägare och fysisk readback.
Inget av detta är godkänt av denna ADR. **NO-GO kvarstår**.

Relaterat: [etapp 3-grindar](../modbus-broker-readonly-stage3-2026-11-02.md)
och [migrationsplanen](../plans/modbus-broker-2026-11-01.md).
