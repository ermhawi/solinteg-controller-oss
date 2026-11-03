# Sundsvall: skrivskyddad säkerhetsinventering och fallback-matris – 2026-11-03

**Status:** Underlag för beslut, **inte** godkännande av fysisk broker-/watchdog-cutover.
**Drift:** Befintlig direktpoller, dispatcher, webb, ordinarie watchdog och
Dell-baserad **observer-only** broker-watchdog lämnades oförändrade.
Ingen ny Modbusklient, FC03/FC04/FC06, systemd-ändring eller
växelriktarkonfiguration genomfördes i denna inventering.

## 1. Faktisk installation och evidens

| Sak | Värde | Evidensnivå / begränsning |
| --- | --- | --- |
| Växelriktarmodell | Solinteg MHT-10K-25 | Sundsvalls installations-/ADR-underlag, **inte** ny ID-registerläsning |
| Driftläge | `0x0303` / EMS BattCtrl | Aktuell `live.json`, 2026-11-03; senaste läge, inte säkerhetsgaranti |
| SoC och batteri-W | Färska men ej publicerade | Driftdata hålls i skyddad lokal `live.json`/SQLite, inte i publikt Git |
| SoC-skydd on-grid, `52502` | `1` = på | **Fysisk FC03-readback 2026-11-02**, inte omläst nu |
| OnGrid EndSOC, `52503` | `100` rått = **10,0 %** | **Fysisk FC03-readback 2026-11-02**, inte omläst nu |
| BMS-laddningsströmgräns, `33021` | Dynamiskt readback finns från 2 november | Värdet ändras med SoC/temperatur; ej nymätt eller publicerat |
| BMS-urladdningsströmgräns, `33023` | Dynamiskt readback finns från 2 november | Värdet ändras med SoC/temperatur; ej nymätt eller publicerat |
| Firmware ARM/DSP och datalogger | **Okänd / inte verifierad** | Inga läsbara versionsuppgifter i den aktiva pollerns publicerade telemetri |
| Batteri/BMS modell och firmware | **Inte oberoende identifierad** | Sunwoda 3 × 5 kWh i installationsunderlag |
| Installerad mjukvarugräns | 12 % | Whitelistad `solinteg.env`-kontroll, 2026-11-03 |
| Ekonomisk planerreserv | 20 % | Whitelistad `solinteg.env`-kontroll |
| Dispatchens arming | `SOLINTEG_CONTROL_ARMED=1` | Aktuellt installerat miljövärde |
| Broker och SoC-stale-grind | Ej aktiva | `solinteg-broker-readonly.service` inactive; broker-flaggorna ej satta |

**Drift kontra Git:** Installerad dispatcher/poller har tidigare konstaterats
skilja sig från `Sundsvall`-källkoden; nya offline-säkerhetsgrindar
ska **inte** antas vara installerade. `MODBUS.md` är delvis gemensam
med uppströms **MHT-20K-40** och referensprover. Resultat på annan
modell, batteri och firmware är **inte verifiering av MHT-10K-25**.

### 1.1 Läs- och kommunikationsbegränsning

Projektets gemensamma `MODBUS.md` beskriver att en tidigare
skrivskyddad probe av identitetsregistret `10008` och `10105`
på **referensinstallationen** misslyckades på FC03/FC04 och
följdes av omkring **70 sekunders Modbusavbrott**.
Gatewayen stöder inte tillförlitligt samtidig konkurrerande TCP-access.
Vi avstår därför från en ny godtycklig session och från
`10011/10012` (firmwareblocket), trots att de förekommer i
andra Solinteg-registerkartor. Ingen sådan identitetsläsning
bör göras som rutin mot Sundsvalls aktiva dispatcher.

**Säker komplettering:** Solintegs manual, avsnitt 7.8, anger vägen
**Main Menu → General Settings → System Info → FM Version** på OLED.
Alternativt kan monitoreringsplattformens **read-only Firmware version**
användas för ARM-/DSP-versionerna; dataloggerns revision måste
fastställas separat. Ingen av dessa observationer kräver en parallell
lokal Modbusklient.
Registrera version och aktuell modellrevision som *lokalt,
sekretesskyddat driftunderlag* före tillverkarfråga och något
fysiskt prov. Publicera inte serienummer, installerad IP eller tokens.

### 1.2 Historisk beteendeinventering (SQLite `mode=ro`)

De senaste 24 timmarna kontrollerades via lokal SQLite `mode=ro`.
Historiken innehåller både `EMS BattCtrl` och `General`, samt
SoC under den ekonomiska 20 %-reserven, men inga observationer under
mjukvarans 12 %-gräns. Den verifierar **inte** om native
10 %-gränsen gäller för kvarhängande `0x303`-börvärden.
Antal, exakta SoC- och effektvärden samt individuella förbrukningsdata
stannar på installationsvärden och publiceras inte i publikt Git.
- Två separata `error_revert_failed`: **2026-11-03 02:02 och
  02:13 UTC** (03:02 och 03:13 CET). Ursprunglig Modbusåtgärd
  misslyckades och återställningsförsöket till General också.
  Telemetriavbrott omkring **140 s respektive 132 s** kring
  händelserna; senare `applied`-rader och normal telemetri
  visar återhämtning, **inte bekräftat säker fysisk lägesövergång**.
  Trolig nätverks-/dongelkoppling återstår att utreda separat.
- Den installerade gamla watchdoggen försöker fortfarande
  `return_to_auto` på gammal heartbeat. Observationspiloten
  gör **inga** registerskrivningar. Den nya säkerhetsmodellen är
  ännu endast i utvecklingskod.

## 2. Vad tillverkaren faktiskt dokumenterar

- Officiella MHT-4~20K-manualen innehåller **MHT-10K-25** och menyerna
  `OnGrid SOCProt` och `OnGrid EndSOC` under avancerade inställningar;
  `FM Version` finns under systeminfo. Manualen visar
  funktionernas existens, **inte** ett bevis för verkställighet av
  ett gammalt `50207` i `EMS BattCtrl`.
- Solintegs OpenAPI Appendix listar `gridConnectedSocProtection`
  (0/1), `gridConnectedDischargeDepth` (10–90 %) och
  en skrivskyddad `firmwareVersion` med ARM- och DSP-delar.
  OpenAPI-värdeintervall och legacy-`MODBUS.md` (5–100 %)
  **skiljer sig**; ändra inga gränser på antagen gemensam semantics.
- Tillverkaren beskriver EMS Battery Control som *direkt*
  effektstyrning med höga nätverkskrav och rekommenderade
  regelbundna uppdateringar 5–10 s, men **ingen bekräftad lokal
  TTL/automatisk retur till General** för just denna firmware.

Källor:
- [Solinteg MHT-4~20K User Manual, avsnitt 7.8 firmware via OLED (MHT-10K-25 ingår)](https://solsol.eu/file/view/2507/solinteg_userman_mht-4_20k-25_en.pdf)
- [Solinteg OpenAPI – inverterkonfigurationer](https://apidocs-en.solinteg-cloud.com/en/docs/appendix/inverter-configurations/)
- [Solinteg OpenAPI – EMS Mode](https://apidocs-en.solinteg-cloud.com/en/docs/solutions/ems-ems-mode/)
- [Lokal EMS-/General-riskanalys](architecture/adr-2026-11-02-ems-general-failsafe.md)
- [Offline-nödfallbackgrind](modbus-broker-emergency-policy-offline-2026-11-03.md)

## 3. Jämförda fallbackstrategier

| Alternativ | Möjlig nytta | Oförändrad risk | Beslut 2026-11-03 |
| --- | --- | --- | --- |
| **A. General `0x101`, befintligt 10 %-golv** | Intern reglering och solupptagning utan Dell | Urladdning under ekonomiska 20 %; verklig effekt och gräns vid fel ej verifierade | **NO-GO** för automatisk reservbevarande fallback |
| **B. EMS BattCtrl `50207=0` med öppna AC-caps** | Begär ingen batteriurladdning | Ingen verifierad noll effekt på denna firmware, kan blockera PV-inlagring; kräver exklusiv write | **NO-GO** tills hög-SoC-prov och feltest |
| **C. Full EMS-freeze, `50207/50208/50209=0`** | Skulle kunna parkera batteriet | Referensprover visar att register-readback kan ge motsatt fysisk effekt | **Avråds** från användning utan ny fullständig validering |
| **D. General med verifierat höjt native-golv** | Kan autonomt respektera vald reserv | Kräver ändrad inverterkonfiguration, verifierat skydd i General och bekräftad SoC-funktion i EMS | **Designkandidat, ej godkänd konfigurationsändring** |
| **E. Native ToU eller EMS General `0x302`** | Eventuell självständig plan/fallback | Firmwarestöd, schema, klocka, timeout, beteende och BMS-verkan okända | **Utredning**, ej fysiskt test |
| **F. Inga nya kommandon; incidentlatch och larm** | Undviker split-brain-skrivare | Gammalt `0x303`-börvärde kan fortsätta urladda eller blockera PV | Säkrast *vid okänd ägare*, men **inte fullständig fysisk återställning** |

**Viktig princip:** 20 % är ekonomisk reserv, 12 % är installerad
mjukvarugräns, 10 % är senast lästa native EndSOC. Skillnaden är
medveten, men en ny automatisk General-fallback skulle kunna
konsumera reserven. Vilket riskavsteg som är acceptabelt måste
beslutas separat; ett positivt `register readback` är inte
bevis på effekt eller BMS-skyddsverkan.

## 4. Verifieringsmatris – inga fysiska test utförda

| Scenario | Offline-felinjektion | Senare fysisk, godkänd verifiering | Acceptanskriterium |
| --- | --- | --- | --- |
| 1. Broker dör under hög EMS-urladdning | Brokern stoppas syntetiskt mellan varje registersteg | Endast hög, trygg SoC; uppmätt batteri-W, mode, caps, SoC över tid | **Inget kvarhängande högt urladdningsbörvärde**; faktisk effekt följer säker strategi |
| 2. Broker/Dell förlorar IP/Wi-Fi | Timeout, partiell TCP, offlinevärd, reconnect, halvöppen socket | Mät observerbarhet; ingen försämring av befintliga skydd | Ingen dubbel Modbusägare, definierat degraderat tillstånd; larm och tidsbudget mätta |
| 3. Låg SoC / okänt SoC | Stale/future/replay, BMS-larm, 10/12/20 %-gränser | Ingen avsiktlig körning ned mot 10 % eller BMS-trip | **Ingen automatisk General/charge/discharge** utan oberoende godkänd evidens |
| 4. General vid solöverskott, sedan mörker | Simulera PV-kollaps under rollback | När godkänt: hög SoC, kontrollerad PV/last, oberoende mätning | Skyddad SoC, sol laddar före export när möjligt, ingen oavsiktlig fas-/säkringsbelastning |
| 5. EMS 0 W / caps | Fake-registerläsning och olika skrivordningar | Separat prov över säkert SoC, med sol och utan sol | Batteri-W inom fastställd tolerans; inget dolt nätköp eller försämrat skydd |
| 6. Flera Modbusklienter | Broker, poller, dispatcher och root supervisor startar samtidigt syntetiskt | Kontrollerad migrering, inte parallell extra TCP-probe | Exklusiv ägare genom start, crash, restart och handover; rå FC06 från andra blockerad |
| 7. Misslyckad revert/readback | Connection refused, timeout, write error, återhämtat svar | Läs av faktisk state efter *separat godkänd* feltest | Status `unverified` kvarstår tills fysisk readback/effekt verifierats; ingen blind success |
| 8. Host strömlös | Simulerad avsaknad av alla Dell-heartbeats | Inte med Dell som enda säkerhetsnod; framtida Pi/nätverksdesign | Extern övervakare/fail-safe-hårdvara, inte Dell-process, täcker totalbortfall |
| 9. Återstart efter incident | Persistenta incidentlås, gamla kommandon, gammal lease | Operativ rollback med säker baseline och explicit kvittens | Ingen automatisk återarming eller replay efter fel; färskt bevis före kontroll |

**Grindar inför varje fysiskt test:** identifierad modell och exakta
ARM-/DSP-/BMS-versioner; skriftligt modespecifikt tillverkarbesked;
färskt *oberoende* SoC/BMS/CT/mode-underlag; endast en TCP-ägare;
installationssäkringar/effektbudget och rollback dokumenterade;
godkänd stoppkondition, ansvarig operatör och aktiv observerbarhet.
Inget test provocerar BMS-låsning, SoC under säker nivå eller
överströmsutlösning.

## 5. Åtgärdsordning / Go–No-Go

1. **Komplettera firmware och modell** via OEM OLED/monitorering
   (read-only), samt BMS-identitet. Undvik osäkra `100xx`-prober.
2. **Tillverkarsvar specifikt för MHT-10K-25 och firmware**:
   gäller `52502/52503` under `0x303` vid uteblivet `50207`?
   Är det någon *lokal* Modbus TTL/EMS-watchdog? Vad gör
   `CommsWatchDog` i en ensam inverter? Vilken neutral mode-/caps-
   sekvens är verifierat säker efter partiell FC06?
3. **Utred nätverksavbrotten 03:02/03:13 CET separat.** Orsaken kan
   vara Wi-Fi, dongelns cloud-blockering, TCP-sessionskonkurrens
   eller annan nätväg. Det är inte identifierat i denna rapport.
4. **Offlineprova samtliga failure cases**, särskilt gamla tvingade
   `0x303`-mål och timeout efter första/andra FC06.
5. **Separat Go/No-Go för hög-SoC-fysiska prov**, med
   dokumenterade stopp-/rollbackkriterier. Ingen provning under
   native SoC-golv, ingen ändring av `52502/52503` i denna etapp.

**Beslut i nuläget:** **NO-GO** för att automatiskt välja General,
0 W-hold eller full freeze som fysisk nödfallback. Ingen verifierad
strategie neutraliserar både ett gammalt EMS-börvärde och skyddar
reserven utan nya antaganden. Fortsätt med befintlig drift, den
skrivskyddade observerpiloten och skriftliga tillverkaruppgifter.
