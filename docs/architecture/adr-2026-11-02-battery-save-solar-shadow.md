# ADR 2026-11-02: solöverskott under tvingat batterisparläge

**Status:** Implementerat endast som offline-/read-only-skuggbedömning. **Ej driftsatt; fysisk aktivering NO-GO.**

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
