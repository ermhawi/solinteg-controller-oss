# Empirisk EVCC/Volvo-kalibrering av AC-kWh per SoC-punkt (2026-11-04)

**Status:** Genomförd **empirisk uppskattningskalibrering**, inte
oberoende AC-kWh-mätarkalibrering. Endast befintliga read-only
historikdatabaser användes; inga nya tjänster, timers, EVCC FC10-
kommandon, inverter-/Volvokommandon eller ändringar i laddplanen.
`physical_control_eligible=false`; `strict-session.json` skapas
eller uppdateras **inte**. Automatisk EVCC-prisstyrning är NO-GO.

## Dokumentationspreflight och källor

- Aktuell pris-/pausägar-ADR:
  [prisplan och aktiveringsgrindar](architecture/adr-2026-11-04-ev-price-plan-and-chart.md).
  `read_ev_session()` kräver fortfarande
  `energy_requirement_quality=independent_ac_calibration` och
  `observation_time_semantics=verified_physical_sample`.
- `ev-ac-estimate.sqlite`: EVCC:s observerade, separata Ict2-ström
  med **antagen** 230 V och effektfaktor 1.0. Integralen
  täcker endast successiva giltiga C2/6 A-prover (max 45 s mellan).
- `ev-history.sqlite`: HA:s rapporterade Volvo-SoC och API-
  ändringstider, `volvo_quality=api_recent_field_change_unverified`.
  `last_changed` är inte ett bevis på en fysiskt tidsstämplad
  BMS-sampling.
- Home Assistant-inventering: 642 entiteter kontrollerades via
  befintlig skyddad GET. Ingen identifierad **dedikerad**
  kWh-räknare för EV-laddaren; husets kWh-entiteter och smartpluggar
  tillhör inte verifierat laddboxens separat mätta krets.
  Carlo Gavazzi EM111 använder enligt installationsdokumentationen
  en separat RS485 **METER**-buss, inte EVCC/RFID-Waveshare.
- Volvos tillgängliga `charging_power_fresh` (1 392 W) hade
  vid kontrollen cirka 53 min gammalt källvärde. Ett sådant enskilt
  gammalt värde kan inte användas som en oberoende integrerad
  AC-kWh-räknare.

## Genomfört prov

En sammanhängande aktiv 6 A-laddningsperiod började
`2026-11-04T09:59:44Z` och fortsatte vid analysens slut
`2026-11-04T21:29:17Z`. Genom EVCC-källans sammanhängande
validerade AC-integraler erhölls cirka **15,806 kWh** uppskattad
delenergi fram till avläsningen. För regressionsprov används
**bara observerade SoC-övergångar inne i perioden**, inte den
första nivån 48 %, vars inträde i heltalsintervallet är okänt.

| Mått | Resultat | Kvalitet |
| --- | ---: | --- |
| SoC-steg för regressionsanpassning | 49–66 %, 18 nivåer | Volvo moln, inte BMS-provtid |
| AC mellan dessa nivåers första observerade ändring | 15,389 kWh | EVCC Ict2 × antagna 230 V/PF1 |
| KWh per procentenhet med ändpunkter | 0,9053 | Empirisk uppskattning |
| Linjär lutning (minsta kvadrat) | **0,8902 kWh/procentenhet** | Empirisk uppskattning |
| `R²` | 0,99798 | Förklaringsgrad, inte meterkalibreringsbevis |
| RMS-residual | 0,208 kWh | Relativt kvantiserad SoC |
| Långa SoC-par, percentiler P10–P90 | 0,8283–0,9247 kWh/% | Spridning, **inte** konfidensintervall |
| Tidigare display-schablon `81.608 / 0.90 / 100` | 0,9068 kWh/% | Okalibrerad |
| Lutning relativt schablon | −1,83 % | Beskrivande jämförelse |

Detta är **två oberoende observerade fenomen** (EVCC-ström
och Volvo SoC), men **ingen självständig verifierad AC-kWh-mätare**.
Den höga linjäriteten avslöjar inte systematisk mätbias i
spänning, effektfaktor, SoC-kvantiseringspunkt, batterivärme
eller AC/DC-verkningsgrad.

**Slutsats:** Den tidigare display-schablonen är ungefärligt
förenlig med observationerna och lämnas oförändrad. Varken
`EV_SHADOW_ASSUMED_USABLE_KWH`, `EV_SHADOW_ASSUMED_AC_EFFICIENCY`,
`strict-session.json` eller någon aktiv laddstyrning ändrades.
Kvalitetsklassen är `empirical_candidate_only` och
`calibrated_ac_kwh_per_soc_pct=null`.

## Reproducerbar körning och tester

Inga nya timers krävs; befintliga EVCC-/Volvo-historiker fortsätter
samla data.

```sh
sudo -n python3 -B scripts/tools/ev_ac_soc_empirical.py --days 1
python3 -B -m unittest discover -s scripts/tests -p 'test_ev_ac_soc_empirical.py'
```

Analysverktyget väljer längsta observerade sammanhängande
fragmentet, verifierar immutabla AC-intervall och HA-källkvalitet,
kräver minst nio observerade SoC-övergångsnivåer och minst åtta
procentenheters ökning, avvisar SoC-hopp/baksteg och hoppar över
den första *icke observerade* laddstartnivån.
Resultatet innehåller alltid
`independent_ac_meter_verified=false`,
`calibrated_ac_kwh_per_soc_pct=null`,
`physical_control_eligible=false` och `device_writes=0`.
Det skriver inga filer och ansluter inte till bilen/laddboxen.

## För att nå verklig kalibrering och GO

1. Läs en **dedikerad, monotont ökande AC-kWh-räknare** för
   endast laddboxen, helst EM111 via den *separata*
   RS485 METER-bussen och en kompatibel read-only-adapter.
   Kontrollera mätarens enhet, upplösning, schema, reset/overflow,
   tändning/lastgräns och tidsglapp. Husets totalkWh är inte
   en EVSE-mätning.
2. Samla minst en sammanhängande laddsession med oberoende
   räknardifferens, EVCC Ict2 och verifierade **fysiska**
   Volvo/BMS-SoC-prov med deras verkliga provtider.
   Eventuella andra bilförbrukare/klimatlast och
   laddningsavbrott måste avgränsas.
3. Härled AC-kWh/procentenhet med felbudget och repeterade
   oberoende intervall. Först efter granskning får data
   överföras till en nytt versionsmärkt, strikt godkänd
   session enligt `read_ev_session()`. Fysiskt
   EVCC FC10-paus-/återstartprov och säkringskvalificering
   är **separata** GO-villkor.

**Rollback / städning:** Endast nya analyser, dokument och tester
är incheckade. Ta bort dem i en vanlig granskad kodändring om de
skulle avvecklas; inga privatinstallerade systemd-enheter,
tillfälliga processer, exporterade SoC-rådata, sekretessvärden
eller nya fysiska prov lämnades efter arbetet.
