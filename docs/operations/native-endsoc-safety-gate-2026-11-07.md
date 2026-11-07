# S09 · Native EndSOC, BMS och firmware — verifierat bevis kontra NO-GO

**Utredningsstatus 2026-11-07: source/tested, fysisk skyddsklassning EJ verifierad.** Inga FC03/FC06-/EVCC-kommandon, ändringar i register `52502/52503` eller systemd har gjorts i denna iteration. Jämför [Solinteg-utredningen 5 november](../solinteg-native-endsoc-review-2026-11-05.md), [EMS-/Dell-bortfalls-ADR](../architecture/adr-2026-11-02-ems-general-failsafe.md), [safety-fencing-ADR](../architecture/adr-2026-11-07-modbus-fencing.md) och källkoden för `inverter_control.py`.

| Skydd | Nivå och belägg | Status och vad den **inte** garanterar |
| --- | --- | --- |
| **Ekonomisk planeringsreserv** | 20 % enligt tidigare installerat planeringsbeslut | Mjuk pris-/cykelregel; fysisk hårdvarusäkerhet **nej** |
| **Dispatcher mjukvarugolv** | 12 % och separat recovery runt 13–16 % i äldre dokumenterade inställningar | Aktiv endast om host, data, styrkedja och broker fungerar; inverter kan fortsätta efter Dell-bortfall |
| **Native On-grid EndSOC** | Historiskt 2026-11-02 läst `52502=1`, `52503=100`; lokalt tolkat som 10 % | **Registerskalning ej tillverkarverifierad**: äldre protokoll anger annan skalfaktor. Historisk läsning bevisar inte aktuell fysisk nivå eller prioritet i EMS 3#3 |
| **BMS** | Historiska dynamiska strömgränser observerade, men exakt BMS-/firmwareversion saknas | Ström-/SoC-skydd vid kvarstående `50207` och hostbortfall är **ej fysiskt kvalificerat** |
| **EMS-börvärde 0x0303** | Solintegs EMS API beskriver extern effektstyrning; installerad broker skickar 3#3 | Ingen bekräftad autonom timeout/lease på vår MHT-10K-25-firmware; **antag inte** General vid död Dell |
| **Legacy watchdog** | Lokal timer och försök till `return_to_auto` vid gammal heartbeat | Konkurrerar potentiellt om TCP; kan inte köras när Dell är strömlös och kan inte förvandla `unknown` till bevisad säkerhet |

## Fail-closed dokumentationsgrind

`scripts/services/native_soc_safety_evidence.py` är en **ren, icke-driftsatt** klassificeringsfunktion. Alla dessa belägg krävs för att **ens få fram `documentary-review-eligible`**:

1. Färsk verifierad `52502` aktiverad och relevant native EndSOC-procent med av Solinteg bekräftad råskalning för **exakt** aktuell ARM/DSP, datalogger och BMS/firmware.
2. Tillverkaren bekräftar uttryckligen om EndSOC stoppar **EMS `50000=0x303`** trots kvarstående positivt urladdningsbörvärde `50207` efter långvarigt nät-/Dell-bortfall.
3. Vendor/BMS-funktion under motsvarande fel är dokumenterad och faktiskt oberoende av Dell; en procent läst ur registern är aldrig ett fysiskt utlösningstest.
4. Verifierad native-procent får **inte** vara lägre än det deklarerade krav som ska behandlas som hårdvarureserv. **Idag är 10 % historiskt native kontra 20 % ekonomiskt planeringsmål**, så ett påstående att hårdvaran garanterar 20 % är direkt **NO-GO**.
5. Även om samtliga dokumentationsbevis i framtiden är uppfyllda ger funktionen **alltid `physical_go=false` och `operator_approval_required=true`**. En separat riskgranskad och bemannad fysisk provplan krävs.

Test: `scripts/tests/test_native_soc_safety_evidence.py`, **4/4** isolerade tester: okänd registerskalning/firmware ger NO-GO, native under ekonomisk reserv ger NO-GO, korrupt/NaN/värde av fel typ ger NO-GO, och komplett hypotetiskt dokumentationsbevis kan aldrig ge automatiskt fysiskt GO.

## Kvarvarande informationsinhämtning

**Exakt ARM/DSP- och BMS-firmware är fortfarande ej verifierade**; använd read-only Solinteg-vy eller inverterdisplay under separat godkänd inspektion. Begär från tillverkaren en **modellspecifik skriftlig beskrivning** av `52503`-skalning, EMS-prioritet, återstart och kvarstående discharge efter kommunikationsfel. Varken publicerad API-parameter eller lyckad simulerad rollback räcker. Nästa praktiska steg är `S10/V01` **planering**; `V04` och en fysisk 20 %-gräns är fortsatt **NO-GO**.

**Omfattning för avslut S09:** risk/utredning och en testad fail-closed **dokumentationsgrind** är klar i källan. **Inte** tillverkarbekräftelse, installationsändring, fysisk verifiering eller tillstånd till release.
