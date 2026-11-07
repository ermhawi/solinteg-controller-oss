# ADR 2026-11-02: solöverskott under tvingat batterisparläge

**Status:** Den separata skuggmodellen i detta dokument är fortfarande endast
read-only och **inte driftsatt som fysisk EMS-`solar_charge`**.
**Senare ändring 2026-11-05:** En annan, centralt samordnad sol-/EMS-prioritetsregel
har driftsatts selektivt i ordinarie dispatcher med General/EMS-växling;
se [den nya ADR:n](adr-2026-11-05-energy-priority-arbiter.md). Denna
skuggmodells tidigare fysiska NO-GO avser fortfarande dess särskilda
EMS-solbörvärde, inte den nya releasevägen.

## Bakgrund och avgränsning

Planeraren markerar ekonomiska idle-intervall med **batterySaveDriftKwh**.
Den aktiva dispatchern omsätter markören till en liten tvingad urladdning
i EMS BattCtrl (0x303), för att undvika att General/Auto använder
batterireserven till huslasten när priset talar för att spara energin.
Denna gren hoppar över live-discharge-spårningen, vilket kan blockera
batteriets laddning från ett faktiskt solöverskott. Detta är en känd
kompromiss, inte ett beslut att aktivt sälja all solel.

Befintlig [EV/L3-skuggmodell](adr-2026-10-31-ev-solar-shadow.md)
utvärderar framför allt Auto/idle vid verifierad EV-laddning. Den
täcker inte en faktiskt beordrad batterispar-urladdning när bilen
kan vara pausad. Den nya modellen är en separat komplettering.

## Read-only-modell

En ren funktion i **scripts/services/battery_save_solar_shadow.py**
anropas av den befintliga
**scripts/services/ev_grid_priority_shadow_observer.py** enbart i
dess read-only-flöde. Den ger **battery_save_solar** som en separat
JSON-sektion, utan något register, effektbörvärde eller kommando.

- Proveniens: senaste kontrollrad ska vara **discharge**, **applied**,
  **armed=1**, med ett positivt **power_w** exakt lika med det
  strukturerade **detail_json.battery_save_w**. Vanlig urladdning,
  misslyckad styrning eller enbart ett 50 W-värde duger inte.
  Loggens **applied** är inte oberoende fysisk readback.
- Driftläge: verifierat **0x303**, balanserad växelriktarutgång
  (**grid_unbalanced_output_raw=0**), färsk live-telemetri (0–60 s),
  rimlig AC/PV/batteribalans och konsistenta tre CT-faser.
- Stabilitet: minst fyra sammanhängande observationer under cirka
  90 sekunder, 20–50 s mellan, senaste högst 65 s gammal, och
  **separata strikt stigande ursprungliga inverter_at_utc**. En
  uppdaterad historiktidsstämpel på samma gamla läsning är inte bevis.
  Kontrollen måste ha varit aktiv under hela fönstret och minst 30 s extra.
- Sol: i samtliga historikprover minst **400 W PV minus huslast**,
  minst **250 W positiv nätexport**, och batteriurladdning 0–500 W
  (tillåtna värden ned till -150 W för mätbrus). Vid tydlig
  laddning behålls den observerade laddningen. Vid stark
  urladdning trots överskott blir svaret en anomali, inte Auto.
- SoC: planerreserv, recovery-entry och recovery-exit måste
  anges uttryckligen; annars **thresholds_unverified**.
  SoC ska ligga minst **0,5 procentenheter över både planerreserv
  och recovery-exit**. För låg SoC ger **reserve_priority**.

Vid samtliga verifierade villkor ger modellen endast
**solar_capture_candidate** och
**hypothetical_mode=review_pv_capture_handoff**.
Varje resultat är märkt **model_actionable=false**,
**physical_control_authorized=false**, **device_writes=0** och
**pv_to_battery_causality_proven=false**. Det finns inget
alternativt "Auto är säkert"-påstående.

Observatören accepterar de tre procentgränserna via CLI-flaggorna
**--planner-reserve-pct**, **--recovery-entry-pct** och
**--recovery-exit-pct**. Inga defaultvärden har uppfunnits.
Vid observerad urladdning kan husbatteriets bedömning göras även när
EVCC-filen saknas eller är ogiltig; då förblir övergripande EV-rådet
**unavailable** medan den separata **battery_save_solar**-sektionen
redovisar husets bedömning. Auto/idle-rådet kräver fortfarande EVCC.

Installerad observatör och dess systemd-timer är **inte** uppdaterade.
Om parametrarna saknas kan modellen inte utfärda en solkandidat.

## Regression och giltighet

Syntetiskt fall efter observationen 2 november: 2 250 W PV,
850 W huslast, 1 550 W export, 150 W batteriurladdning och
22 % SoC. Testet matar in hypotetiska gränser 20/19,5/21 %
för att verifiera klassificering och spärrar; **det är inte en
verifierad historisk fyrprovsreplay eller bekräftade driftgränser**.

Relevanta tester är
**scripts/tests/test_battery_save_solar_shadow.py** och
**scripts/tests/test_ev_grid_priority_shadow_observer.py**.
De testar bland annat gammal/återanvänd telemetri, SoC-reserv,
okänd lägesreadback, bristande CT-konsistens, ofullständig historik,
EV-oberoende observation och obestyrkt 50 W-urladdning.

## Verklig historisk återspelning och reserver (2026-11-02)

Det isolerade verktyget **scripts/tools/replay_battery_save_solar.py**
läser **telemetry.db** och **ev-history.sqlite** med SQLite
**mode=ro**, inklusive WAL. Det kräver exakta, överensstämmande
ursprungstider (**inverter_at_utc = readings.timestamp**) och
kontrollerar att mätvärdena verkligen matchar. Det räknar
**senaste** kontrollrad, även misslyckad eller oarmerad, i stället
för att hoppa tillbaka till ett äldre lyckat börvärde.
Verktyget importerar ingen hårdvarustyrning och sparar inga rådata.

Med explicit verifierad driftkonfiguration **15,055 kWh**,
planeringsreserv **20 %**, recovery-entry **19,5 %** och
recovery-exit **21 %** gav den första skrivskyddade återspelningen
för 2 november kl. 09:00–13:07 UTC:

- 493 historikprover; 492 matchades exakt mot ursprungliga
  inverteravläsningar. 81 prover avsåg ett identifierat, armerat
  och loggat 50 W-batterisparläge.
- **0 strikt verifierade solkandidater:** historiska mätserier
  saknar inverterregister **50006**. Tre CT-kanaler bevisar inte
  själva växelriktarens lägesinställning.
- **18 hypotetiska kandidater i tre perioder** kl. ungefär
  11:48–12:00 UTC, *endast under det uttryckliga antagandet*
  att 50006 var 0. Dessa ger **ingen fysisk skrivbehörighet**.
- Vid den verkliga Auto-handoffen omkring 12:01 UTC observerades
  batteriladdning. Det verifierar en historisk laddningseffekt,
  **inte** framtida skydd när solen försvinner eller lasten ökar.

Stresstestet använder **5 363 W** som ett faktiskt observerat,
men inte certifierat maximalt, batteriurladdningsfall och
**120 s** som en helt **hypotetisk** summa av detektion/åtgärd.
Vid 22 % SoC återstår cirka **50,5 s** till modellens
granskningsgräns 21,5 % om denna urladdning fortsätter.
**12 av 18** hypotetiska kandidater klarar därför *inte*
reaktionsbudgeten. Vid lägre antagen urladdning kan stresstestet
ge **physical_safety_unverified**, aldrig automatisk behörighet.
Sann maximal urladdning och faktisk oberoende supervisors
reaktionstid är ännu inte verifierade. Även SoC visas i historiken
i grova heltalssteg, så 50,5 s är **inte** en garanterad
minsta skyddsmarginal: den faktiska marginalen kan vara mindre.

Exempel på ett **rent offlineprov** mot privata databaser
(UTC och beloppen är provets givna parametrar, inga koddefaults):

```sh
sudo -n -u solinteg-evshadow python3 -B scripts/tools/replay_battery_save_solar.py \
  --since 2026-11-02T09:00:00Z --until 2026-11-02T13:07:00Z \
  --planner-reserve-pct 20 --recovery-entry-pct 19.5 --recovery-exit-pct 21 \
  --capacity-kwh 15.055 --stress-discharge-w 5363 --response-budget-s 120
```

**scripts/tests/test_replay_battery_save_solar.py** provar
offline: exakta timestamps, återanvänt/gammalt prov, tappad
CT-mätning, styrfel, okänd mode, PV-kollaps, SoC-prioritet,
historisk Auto-sekvens och stressmarginal. Även när solen
försvinner slutar skuggmodellen föreslå övergång, men den
**kan inte fysiskt avbryta ett redan återställt Auto-läge**.

## Säkerhetsgrind inför eventuell fysisk styrning

Den 1 november föll SoC efter att Auto återinförts.
Auto under sol kan därför inte tillåtas automatiskt bara på grund
av aktuell export; solinstrålningen och lasten kan ändras snabbt.
Innan ett separat godkänt fysiskt prov krävs:
bevisad solbevarande och reservskyddad övergång på denna
växelriktare, stabil väder-/SoC-hysteres, oberoende BMS/SoC-
och RMS/fasskydd, exklusivt Modbusägarskap, fysisk readback,
avbrott av redan aktiva tvångsbörvärden samt en verifierad
oberoende säkerhetssupervisor/watchdog.

**Ingen ändring** har gjorts i den aktiva dispatchern, pollern,
watchdoggen, EVCC, gate eller systemd. Read-only-implementationen
är endast utvecklingskod.

Relaterat: [SoC-handoff](../soc25-handoff-shadow-2026-11-01.md),
[brokerstale/SoC](adr-2026-11-02-stale-telemetry-soc.md),
[EV/L3-skuggmodell](adr-2026-10-31-ev-solar-shadow.md).

## Tillägg 2026-11-02: direkt EMS-solupptagning (endast källkod)

En separat ändring i `scripts/services/dispatch_loop.py` ger ett
**valfritt, direkt** `solar_charge`-beslut under en redan vald
`battery_save_w`-urladdning. Detta är **inte** ett nytt shadow-läge,
en ny timer eller en separat styrprocess. Tidigare read-only-modell
och dess NO-GO för automatisk övergång till General är oförändrade.

Beslutet bevarar EMS BattCtrl (`0x303`) och begär en **negativ**
`50207`-laddning via befintlig `force_charge`, utan övergången
till General. Det kräver `DISPATCH_BATTERY_SAVE_SOLAR=1`, aktivt
`DISPATCH_L3_CHARGE_GUARD_MODE=1`, högst 45 s gamla,
konsistenta fas-/nätmätningar, balanserad utgång (`50006=0`),
bekräftat EMS-läge, faktisk export, överskott `pv_w-house_load_w`
och SoC under befintligt tak. Laddbegäran begränsas av minsta
PV/hus- respektive nät-/batterimätning minus **500 W marginal**,
av verkningsgrad och av befintliga effekt-/L3-gränser.
Nedjustering och övergången tillbaka till ordinarie 50 W-sparläge
kan ske i samma 15-minutersslot; SoC-recovery och planerad laddning
har fortsatt företräde. `solar_charge` och ingångsvärdena loggas
till `control_actions.detail_json` och namnges separat på webben.

**Säkerhetsbegränsning (fortsatt NO-GO för produktion):**
`force_charge` öppnar nätimport och arbetar med ett fast börvärde
fram till nästa dispatch-/pollerprov. En 500 W-marginal kan **inte**
garantera noll nätfinansierad laddning om moln eller huslast ändras
snabbare än samplings-/styrintervallet (30 s poller + 60 s dispatcher,
plus skrivlatens). I sju dagars historiska mätningar fanns 103
relevanta export-/EMS-observationer med ca 60 s efterföljande mätning;
22 av dem tappade mer än 500 W PV-minus-huslast på en minut, med
ett största tapp på 4 095 W. Detta är en offline-riskindikator,
inte ett test av det nya börvärdets fysiska beteende.
BMS egna dynamiska strömgränser måste fortfarande
bekräftas på hårdvarunivå; denna ändring läser inte ut dem som en
oberoende programvarubegränsning. Den nätbaserade site-importgränsen
skyddar huvudsäkringen, inte den ekonomiska regeln `endast sol`.

Därmed är **`DISPATCH_BATTERY_SAVE_SOLAR` av som standard** och
varken installerad dispatcher, env-fil, kontrollregister, EVCC eller
systemd har ändrats. En fysisk aktivering förutsätter att dessa
konkreta begränsningar kan accepteras/lösas och att den installerade
kodens separata avvikelser mot Git granskas. Det finns inga krav på
en obligatorisk shadow-fas eller ny watchdog.
