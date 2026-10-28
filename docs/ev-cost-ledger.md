# XC40 energikostnad per mil – read-only bokföring (utvecklingsläge, 2026-10-28)

Se även designspecifikationens avsnitt 8.5 i
`docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md`.

## Status och gränser

Den verkliga kostnadsbokföringen (`kr/mil`) är **ännu inte driftsatt**.
Däremot är två isolerade, read-only **bevisinsamlare** aktiverade på Dell:
anonymiserad körjournal från Volvo/HA och oavstämda historiska
elprismodeller. Inga bevis omvandlas automatiskt till debiterade
laddsessioner eller körkostnader. Ingen liveändring har gjorts i laddbox,
Volvo, växelriktare eller Solintegs dispatch.
Dagens `ev_history.py` har 30-sekundersobservationsdata men saknar
verifierade kWh från EM111 och exakt bilbatteri-DC-ökning.
Tidsmatchade, anonymiserade körintervall finns numera i en **separat
bevisdatabas**, men deras bilrapporterade förbrukning är ännu inte
verifierad nettourladdning av DC-energilagret. Därför är en tom
kostnadsbokföring **inte** en uppgift om `0 kr/mil`. Låt befintlig XC40-graf vara oförändrad tills
fullständiga eller tydligt märkta uppskattade dataposter kan produceras.

## Implementerad avgränsad modell

- `scripts/services/ev_cost_ledger.py`: separat SQLite `ev_cost_events`,
  `PRAGMA user_version=1`, append-only källhändelser med idempotenta
  `event_id`. Skriver **aldrig** i husets telemetridatabas. Validerar
  tidsstämplar, energibalans, källor och att förbrukning inte kan överskrida
  bokfört batterilager. Rekonstruerar lagret deterministiskt från händelser.
- Energikällorna `grid`, `solar`, `public`, `unknown` har separata
  AC-kWh och historiska källspecifika priser i öre/kWh. Solel ska matas in
  med utebliven **nettoexportintäkt**, endast för faktiskt tillgängligt
  överskott efter huslast/husbatteri; detta kan vara negativt. Koden
  **gissar inte** fördelningen från netto-W på L3.
- Lagret innehåller `stored_dc_kwh`, känt värde och okänd energi.
  AC-laddförlust kapitaliseras i priset på lagrade DC-kWh **en gång**.
  Direkt klimatisering under laddning kan undantas med
  `direct_aux_ac_kwh` (proportionellt källfördelad). Dess kostnad
  rapporteras separat via `direct_aux_cost_ore`; okänd andel ger `null`.
  Förvärmning/parkering bokförs separat med `type=aux` och ingår inte
  i `kr/mil`.
  Initialt okänd inköpskostnad, oregistrerad publik laddning och
  ofullständiga priser förblir `partial` och ger `cost_ore=null`.
- `type=drive` belastar lagret med då gällande vägt anskaffningspris.
  Summan av körkostnader divideras med **summan av körda mil**, inte
  oviktat medelvärde av flera `kr/mil`.
- Händelser och beräknad revision lagras separat; korrigering sker via
  ny `type=adjust`, aldrig genom att ändra historiska råhändelser.
  Osäker positiv SoC-korrigering lägger till *okänd* energi.
- Läs-endpoints `/api/ev/cost/summary` och
  `/api/ev/cost/driving?from=<UTC>&to=<UTC>` läser en atomiskt
  exporterad JSON-snapshot. De är read-only, returnerar
  `Cache-Control: private, no-store` och kräver en separat
  minst 32 tecken lång `Authorization: Bearer`-nyckel.
  Frånvaro av rätt konfiguration, fil, schema eller behörighet stänger
  API:t utan att visa resor. Ingen POST/PUT-rutt finns för bokföringen.

## Driftsatt read-only källadapter: Volvo-körjournalens verkliga intervall

En separat adapter i `scripts/services/ev_cost_capture.py` kan läsa
`GET /api/volvo_trip_energy/cost_segments` från befintlig
`volvo_trip_energy`. Den skyddade HA-vyn **driftsattes 2026-10-28**
via GitOps `ermhawi/ha-kube@cb2fc0d` (ursprungsrevision
`30a4086`). ArgoCD rapporterade Synced/Healthy och HA:s
konfigurationskontroll passerade efter omstart. Anonym och ogiltig
Bearer-token fick 401; korrekt server-till-server-token fick 200,
`application/json`, `Cache-Control: private, no-store` och
ett godkänt versionerat datakontrakt.

Till skillnad från den befintliga dashboardens rullande `bins_30d`
returnerar routen verkliga **anonymiserade tidsintervall** från Volvos
privata körjournal, utan VIN, GPS, trip-ID eller kilometerställning.
Adapter och HA-källa delar ett versionsmärkt kontrakt. Den avvisar
stale/partial-källa, felaktiga tidsstämplar, omöjliga energivärden,
överlappande intervall och nya persondatafält. Med samma
`HA_API_URL` och `HA_READONLY_TOKEN_FILE` som befintlig säker
HA-läsare utförs endast ett begränsat `GET` till privat HA IP,
utan omdirigering eller systemproxy.

```bash
# Utan --capture: verifierar kvalitet och räknar underlag, skriver INTE till disk.
python3 scripts/services/ev_cost_capture.py --ha

# Separat explicit aktivering för att *enbart* spara anonymiserat
# körunderlag i egen SQLite efter att skyddad HA-rutt har driftsatts:
EV_COST_CAPTURE_ENABLED=1 python3 scripts/services/ev_cost_capture.py --ha --capture
```

`--capture` skriver endast till en separat privat SQLite
(`EV_COST_EVIDENCE_DB_PATH`, default `var/ev-cost-evidence.sqlite`)
och är idempotent mellan avläsningar. En ändrad eller överlappande
historisk rapport avvisas utan att tyst uppdatera gammal mätning.
Allt rapporteras som `reported_consumption_kwh` och
`cost_quality=unavailable`; **inga `drive`-händelser överförs ännu till
kostnadslagret**, eftersom Volvo-data enligt integrationens källtolkning
är rapporterad bruttoförbrukning, inte verifierad nettourladdning ur
batteriet efter regenerering. Det vore fel att debetera denna skillnad
som om den vore köpt energi. En separat avstämd DC-energimodell och
registrerade AC-laddsessioner/priser återstår.

Efter genomförd HA-granskning är två **bevisinsamlings-timers**
driftsatta; den faktiska DC-energi-/kostnadsledgern och
`kr/mil`-API:t är fortfarande inte aktiverade. Inga fysiska
laddningskommandon har tillåtits. Återställning av den nya
HA-vyn kräver en ny GitOps-commit mot tidigare revision; separat
Dell-rollback står nedan.

## Utförd read-only provinsamling och avgränsad prislogg, 2026-10-28

Utan HA-koddeploy/routestart kunde den befintliga privata HA-cachen
`.storage/volvo_trip_energy_snapshot` läsas **en gång** via Kubernetes
read-only, och dess uppmätta `last_success` bekräftades mot HA:s
`sensor.volvo_xc40_measured_kwh_per_mile` under samma körning.
Med `--file - --capture` importerades **218** validerade anonyma intervall
(304,392 km och 81,4996 kWh bilrapporterad energi, 18–28 oktober) till
`/opt/solinteg/ev-shadow-runtime/var/ev-cost-evidence.sqlite`. Ingen
källa/ID/GPS/OAuth-token eller råsvar sparades. Databasen skapades av
`solinteg-evshadow`, mode `0600`; `PRAGMA integrity_check=ok`.

En andra, helt separat `scripts/services/ev_cost_price_capture.py` läser
den redan existerande `price_snapshots` via SQLite **mode=ro** och kräver
ett helt dagsdygn med `source=actual` per 15-minutersplats. Lokal svensk
klocktid mappas till **entydiga UTC-intervall**, inklusive dubbla
02:xx den 25 oktober (100 platser) och vårens 92 platser.
Ett begränsat engångsprov importerade **2 884** platser för **30/30**
fullständiga dagar, 29 september–28 oktober, till
`var/ev-cost-price-evidence.sqlite` (mode `0600`,
`integrity_check=ok`). Varje pris är oföränderligt versionsanknutet
till ursprunglig snapshot-hash och UTC-tid; ändring ger explicit fel.

**Prisernas kvalitet är `modelled_day_ahead_excludes_grid_and_unreconciled`,
inte fakturakostnad**: `buy_before_grid_ore` är Solintegs
historiska inköpsmodell före skatt/överföring, `sell_model_ore` dess
modellerade exportvärde. Ingen verifiering mot verkligt Tibber-avtal,
faktura, faktisk nätöverföring eller effekt-/abonnemangsavgift har
skett. Dessa prisbevis får inte automatiskt skapa `charge`-events.

```bash
# Förhandsgranskning: ingen databas skapas/ändras.
python3 scripts/services/ev_cost_price_capture.py --days 30

# Enbart avsiktlig, privat prisbevisimport; aldrig produktionstelemetri-DB:
EV_COST_PRICE_CAPTURE_ENABLED=1 python3 scripts/services/ev_cost_price_capture.py --days 30 --capture
```

## Kontrollerad Dell-aktivering 2026-10-28

Efter HA-driftsättning installerades fyra systemd-enheter från
`deploy/solinteg-ev-cost-{capture,price-capture}.{service,timer}`
och både timers aktiverades som `enabled/active`.
`solinteg-ev-cost-capture.timer` hämtar anonymiserat körunderlag
var 30:e minut (första planerade körningen kl. 20:00 CET);
`solinteg-ev-cost-price-capture.timer` sparar dagens
historiska prismodell kl. 00:20 CET (nästa 29 oktober).
Båda körs som `solinteg-evshadow`, har `EV_ENABLED=0`,
`EVCC_CONTROL_ARMED=0` och saknar hårdvaruskrivfunktioner.
De isolerade tjänsterna har egna explicita
`EV_COST_CAPTURE_ENABLED=1` respektive
`EV_COST_PRICE_CAPTURE_ENABLED=1` så att den ordinarie
`ev-shadow.env` och alla gamla tjänster förblir oförändrade.
Tjänsterna är åtkomstbegränsade via systemd; prisinsamlingen
saknar helt IP-nätverk, medan HA-läsningen bara får ansluta till
uttryckligen tillåtna privata HA-adresser.

Källprogrammen installerades tidigare som `root:solinteg`
mode `0440` med verifierad SHA256. Efter `systemctl daemon-reload`
testkördes båda tjänsterna och gav `Result=success`,
`ExecMainStatus=0`, `device_writes=0` och **0 nyinsatta
dubbletter**. Privat SQLite-kontroll bekräftade 218 körsegment och
2 884 prisplatser, `integrity_check=ok` och mode `0600`.
Samtliga fyra befintliga EV-observatörer var fortsatt `active`.
Endast evidenssamling körs: ingen laddkostnad, fysisk styrning eller
ändring av XC40-dashboarden.

**Avgränsad rollback, påverkar bara de två nya insamlarna:**

```bash
sudo systemctl disable --now solinteg-ev-cost-capture.timer solinteg-ev-cost-price-capture.timer
systemctl is-active solinteg-ev-ha solinteg-ev-shadow solinteg-ev-observer solinteg-ev-history
```

Låt revisionsdatabaserna ligga kvar för granskning; stoppa inte
`solinteg-dispatch`, `solinteg-poller` eller tidigare EV-läsare.
HA-vyn kan separat återställas genom en GitOps-revert mot
`ermhawi/ha-kube` före `cb2fc0d` och en ny konfigurationschecksumma.
Enbart inaktivering av Dell-timers kräver **ingen** HA-rollback.

**Kvarvarande GO-grind:** Ingen laddningssession kunde verifieras i
EVCC-historiken (endast A1-vila eller misslyckade FC03-läsningar).
Vid read-only-felsökning senare samma kväll svarade Waveshare TCP
med `No route to host`; `solinteg-ev-observer` och
`solinteg-ev-history` var aktiva, men senaste
`evcc-readonly.json` hade `read_ok=false` och saknade aktuell ström.
Det tidigare verifierade 6 A-taket får **inte** tolkas som aktuellt
readback när källan är otillgänglig. Ingen anslutnings-/säkrings- eller
laddboxändring gjordes. `EVCC/RFID` och `METER` är olika fysiska
bussar enligt installationsdesignen; EM111 går inte att läsa via
ytterligare en EVCC-slavadress.
AC-kWh per laddning, faktisk andel köpt/sol efter husbatteriprioritet,
avstämd tariff och nettourladdning/DC-verkningsgrad efter regenerering
måste tillkomma. Därför är `kr/mil=null` och `cost_quality=unavailable`
fortfarande **korrekt**, även med 30 dagars kör- och prisbevis.
Husets livereglering, bilens 6 A-gräns och HA-dashboarden ändrades inte.

## EVCC-strömmen används nu för uppskattad laddenergi (2026-10-28)

Efter att användaren kopplade in Waveshare igen och startade
**Volvo XC40:s laddning** kom nya **färska** FC03-svar:
`state=C2`, `configured_pwm_raw=100`, `pilot_pwm_raw=100`
(= **6,0 A**), `channel_currents_a=[0.1,6.0,0.1]`,
`measured_current_a=6.2` (summan av kanalerna). **Den aktiva
Ict2-kanalen är 6,0 A**; Ict1 och Ict3 innehåller cirka 0,1 A
vardera och ska **inte** betraktas som bilens laddström.
Länken svarade också på ett separat TCP-anslutningstest.
Detta ersätter inte tidigare felhistorik utan visar att kommunikationen
återkommit vid det senare observationstillfället.

Enbart `scripts/services/ev_ac_capture.py` läser observerarens
redan tillgängliga `evcc-readonly.json` – **inga egna FC03-anrop**,
inga Modbus-, bil-, HA-, nätverks- eller inverterskrivningar.
En separat isolerad `solinteg-ev-ac-capture.timer` kontrollerar
snapshoten var 15:e sekund men skriver endast **ett** källprov per
unikt observerartidsstämpel (observeraren uppdaterar ungefär
var 30:e sekund). Den privata `ev-ac-estimate.sqlite` innehåller
anonyma råbevis och härledda delintervall som är idempotenta.
Tjänsten körs som `solinteg-evshadow` med nätverk helt förbjudet,
`EV_ENABLED=0`, `EVCC_CONTROL_ARMED=0`, dedikerad
`EV_AC_CAPTURE_ENABLED=1` bara i tjänsten samt privat filmode 0600.
Ingen annan befintlig tjänst behövde startas om.

**Beräkningen är uttryckligen en uppskattning:**
`P_estimate = Ict2_A × 230 V × PF 1,0`. Under den
verifierade laddningen gav detta cirka **1,38 kW**. Summan
`estimated_ac_kwh_covered_only` beräknas genom trapetsintegral
av två **intilliggande**, färska, lyckade, 6 A-verifierade
`C2`-observationer. Mellanrum över 45 s, misslyckade FC03-läsningar,
annan pilot, extra kanallast eller förlorat källtillstånd räknas
**inte** som uppmätt 0 kWh eller fortsatt laddning.
Ingen energi före den första godkända observationspunkten
gissas fram. Detta är ett estimat på AC-energi **till bilen**, inklusive
eventuella ombordladdningsförluster och samtidiga bilförbrukare –
inte en uppmätt EM111-kWh-mängd, faktisk DC-ökning eller fakturakostnad.

```bash
sudo -n -u solinteg-evshadow python3 /opt/solinteg/ev-shadow-runtime/scripts/services/ev_ac_capture.py --summary

systemctl is-active solinteg-ev-ac-capture.timer
systemctl show solinteg-ev-ac-capture.service -p Result -p ExecMainStatus

# Avgränsad rollback: stoppa ENDAST denna nya insamlare
sudo systemctl disable --now solinteg-ev-ac-capture.timer
```

**Första fältverifiering:** efter två godkända C2-observationer
registrerade den nya källan ett 30,05 s långt delintervall,
`0,011518 kWh` uppskattad AC-energi utan fysisk skrivning;
efter tre prover hade den 60,09 s täckning och `0,023036 kWh`.
Det är **inte** total energi för hela laddsessionen, som startade
innan den nya loggern startades. Kvalitet = `estimated`,
`cost_ore=null` och `vehicle_battery_dc_kwh=null`.

**Husbatteriskyddet:** från Solintegs synkroniserade historik
(UTC 19:04–19:09:30 jämfört med 19:10:30–19:16:30) ökade
L3:s nätimport från cirka **76 W** till **1 487 W** (≈ 1,41 kW),
samtidigt som husbatteriets genomsnittliga urladdning var
**224 W** före och **213 W** under laddningen. Detta är starkt
förenligt med att bilen matas av **nätet** utan tillkommande
urladdning från husbatteriet, men är **inte** en direkt mätning
av varje elektrons källa eller garanti under andra belastningar.
L3-värdet är momentan nettoeffekt, **inte** oberoende RMS-fasström
för 20 A-säkringsskyddet. Befintliga begränsningar och skuggläge
behålls; laddboxens 6 A-tak har **inte** skrivits eller ändrats.

**Nästa verifiering:** jämför fortsatta 6 A-prover med samtidiga
L3-deltan och Volvos laddstatus/SoC under ett längre förlopp.
Kontrollera batteri-/laddförlustmodell och faktiska tariffer innan
något `charge`-event med `kr` läggs i kostnadsbokföringen.
EM111 vore en precisionsförbättring men är **inte ett krav** för
att logga tydligt märkt uppskattad AC-energi genom EVCC.

## Sammanhängande laddningsperioder och modellpris (skrivskyddad analys)

`scripts/services/ev_ac_sessions.py` läser nu privat
`ev-ac-estimate.sqlite` och `ev-cost-price-evidence.sqlite` med
SQLite `mode=ro` och utan något API- eller hårdvaruanrop.
Den läser inte Solintegs livereglering och uppdaterar inte någon
källa, enhet, databas eller API. Scriptet finns på Dell i samma
isolerade runtime; **ingen ny timer** behövs eftersom
`ev-ac-capture.timer` redan samlar källdata.

```bash
sudo -n -u solinteg-evshadow python3 \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_ac_sessions.py --days 1
```

Analysen identifierar `observed_charging_fragments` från
**sammanhängande verifierade C2-mätpar**. Ett giltigt par kräver
Ict2, 6 A PWM-råavläsning, ingen onormal Ict1/Ict3-last,
högst 45 sekunders lucka och att den privata databasens lagrade
kWh går att återskapa exakt från råströmmen. En lucka eller
misslyckad avläsning **bryter täckningen**. Om ett färskt
bekräftat icke-laddande tillstånd angränsar får fragmentet markering
för observerad gräns, men den exakta övergångstidens energi är
fortfarande okänd. `full_session_ac_kwh` sätts därför alltid till
`null` i denna version. De före och efter observerad tid
saknade kWh extrapoleras inte.

För varje täckt intervall matchas historiska kvartstimmar **via
UTC**. En gräns mellan två prisperioder hanteras genom
trapetsintegrerad ström på **båda sidor** om gränsen.
`hypothetical_grid_only_spot_model_ore` är **endast ett
kontrafaktiskt räkneexempel** under antagandet att all täckt
AC-energi köpts från nätet till Solintegs oavstämda
inköpsprismodell, utan överföringsavgift och korrekt slutavräkning.
Räkneexemplet är inte en verifikation av faktisk nätandel,
pris på solel, betald laddkostnad, DC-batteriinnehåll eller
`kr/mil`. Om någon kvart eller källmätning saknas blir
pris- eller energivärdet `null`; okänt blir aldrig gratis.

**Första fältprov med prisöverlagring:** kl. 20:29 CET hade
25 källobservationer gett 24 sammanhängande giltiga delintervall
(721,69 s, **0,277319 kWh uppskattad täckt AC**).
Den hypotetiska modellkostnaden för **enbart denna delmängd**
var **9,1737 öre före nät-/fakturakostnader**.
`actual_cost_ore=null`, `full_session_ac_kwh=null` och
`vehicle_battery_dc_kwh=null` är avsiktliga och måste
bestå tills separata mätkedjor/tariffavstämning finns.
Den körande Solinteg-telemetrin exponerar inte RMS L3-spänning
eller effektfaktor, så 230 V/PF1 är inte direkt kalibrerade.
L3-effektens tidigare före/efter-ökning cirka 1,41 kW
stämmer med cirka 1,38 kW från EVCC, men den
jämförelsen kan påverkas av samtidig huslast och får inte
användas som exakt spännings- eller kWh-kalibrering.
Som ytterligare kontroll visade HA:s `sensor.volvo_xc40_charging_power`
**1 380 W**, exakt samma nominella effekt som 6,0 A × 230 V,
samt `charging` och batteri-SoC **58 %** efter tidigare 57 %.
HA:s `last_reported` är ett återrapporterat HA-klockslag:
det bevisar inte en ny, oberoende OEM-effektmätning.
Originalvärdets `last_changed` var äldre än avläsningen.
Volvos tal kan ha samma nominella spänningsantagande och
är därför **inte** en oberoende mätarkalibrering.
Utan verifierat RMS-värde, effektfaktor eller separat kWh-mätare
får uppskattningen fortsätta märkas `estimated`.

Pythonregressioner täcker klockomställningen, prismatchning
vid 15-minutersgränser, saknat pris, negativt modellpris,
varierande ström, saknade/långsamma FC03-prover,
halvöppna sessioner, förfalskade kWh och
källpost med otillåten skrivindikering.

## Verifiering av prisdelar och laddningens mätsäkerhet (2026-10-28)

**Tre prisnivåer måste skiljas:**

1. **Solintegs historiska elhandelsmodell**, från
   `price_snapshots`: spotpris plus ett konfigurerat påslag
   inklusive moms; aktuell aktiv miljö anger
   `SUPPLIER_SURCHARGE_ORE=9.29` **före moms**.
   Priset kan inte ännu jämställas med Tibbers fakturerade pris.
2. **Sundsvall Elnäts publicerade privatkundstariff för 2026:**
   rörlig överföring **26,30 öre/kWh** och energiskatt
   **45,00 öre/kWh**, båda **inklusive moms**. Totalt
   **71,30 öre/kWh**. Publicerad fast 20 A-avgift
   **8 053 kr/år** ingår **inte** i marginalkostnad per kWh.
   Den befintliga driftskonfigurationen har
   `SKATT_OVERFORING_ORE=71.38`, alltså 0,08 öre/kWh
   högre än dagens publicerade totalsats. Ingen automatisk
   justering eller slutsats om fakturafelsättning görs.
   Källa: https://sundsvallelnat.se/min-anslutning/priser-och-avtalsvillkor/2026-sakringsabonnemang-privat
3. **Tibbers publicerade villkor**, gällande från
   2025-10-30, anger ett fast påslag **6,00 öre/kWh
   exklusive moms**, dessutom **rörliga inköpskostnader**
   samt **49 kr/månad inklusive moms**. En differens mot
   Solintegs 9,29 öre är därför inte i sig ett fel:
   den dynamiska inköpskostnaden, eventuella rabatter
   och kundens faktiska avtal är ännu inte avstämda.
   Källor: https://tibber.com/se/sammanfattning-avtalsvillkor
   och https://tibber.com/se/villkor/elavtalsvillkor

**Ny explicit, alternativ räknevy**:

```bash
sudo -n -u solinteg-evshadow python3 \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_ac_sessions.py \
  --days 1 --published-network-2026
```

`hypothetical_grid_only_public_2026_network_ore` summerar
Solintegs oavstämda historiska **all-grid-modellpris** och
**71,30 öre/kWh publicerad nätöverföring + skatt** på **endast
täckta, verifierade EVCC-delintervall**. Det är ett
**frivilligt kontrafaktiskt scenario**, *inte* faktiskt
fakturerade ören, kundverifierad nätavgift, tillkommande
elhandelsinköpskostnader, fastavgiftsallokering eller
verifierad sol-/nätfördelning. Ingen tariff för andra år
appliceras automatiskt; vid gränsöverskridning,
saknat kvartpris eller tom laddserie är scenariot `null`.
Ordinarie körning utan denna CLI-flagga ändras inte.

**Ny oberoende kvalitetsdiagnostik**, installerad read-only:

```bash
sudo -n -u solinteg-evshadow python3 \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_ac_quality_audit.py \
  --hours 24
```

`ev_ac_quality_audit.py` öppnar den redan körande
`ev-history.sqlite` och EVCC:s `ev-ac-estimate.sqlite`
i **SQLite mode=ro**. Ingen bakgrundstimer, privat token,
styrkommandon, ny historikdatabas, HA-skrivning eller
Solinteg-dispatchändring tillkommer. Den jämför
**färskhetskvalificerade, bilrapporterade SoC-nivåer**
med en **oberoende L3-före/efter-differens** från husets
effektmätningar. Den *beräknar inte* DC-kWh eller
AC/DC-verkningsgrad, eftersom batterikapacitet,
källtid och avrundad procentnivå inte är verifierade
som en exakt kWh-mätare.

Första provet, omkring 20:47 CET:

- **59** färskhetskvalificerade historikposter över EVCC:s
  täckta fragment, med Volvo SoC **57 → 58 %**.
  Ett enda helt procentsteg (avrundningsosäkerhet
  i samma storleksordning) ger **inte** stöd för
  laddförlust- eller batterikapacitetskalibrering.
  `minimum_trend_resolution_met=false`.
- L3:s observerade medianimport ändrades
  **72 → 1 487,5 W**, alltså **+1 415,5 W**;
  den tidigare jämförelsen med 6 A × antagna 230 V
  ger **1 380 W**. Husbatteriets medianurladdning
  var **233 → 213 W**, alltså ingen ökad urladdning
  under just dessa fönster. PV var 0 W.
- Tidigare EVCC-readback var **inte tillgänglig** i
  referensperioden (innan första verifierade C2), så
  exakt laddstart och andra samtidiga hushållslaster
  är fortfarande osäkra. `quality=indicative_only`,
  `source_allocation_verified=false`,
  `independent_fuse_rms_verified=false`.
- Vid denna kontroll gav **60** EVCC-källposter
  **1 773,91 sekunder** sammanhängande observation
  (**0,684700 kWh uppskattad täckt AC**).
  Det opt-in publicerade nätprisscenariot var
  **72,4027 öre**, medan all-grid-spotmodellen
  före nätkostnad var **23,5837 öre**.
  **Inget av dessa belopp är fakturauppgifter.**

**Före ekonomisk aktivering:** samla flera SOC-steg
under fullständigt täckta laddperioder, hitta en verifierad
användbar bilbatterikapacitet eller oberoende mätare,
och hämta verkligt avtals-/fakturapris från
Tibber samt faktiskt nätavtal och eventuella rabatter.
Ingen kundspecifik Tibber-/elnätsfaktura med verifierade
priskomponenter finns ännu i underlaget. En
generell fakturaavisering räcker inte för
individuell prisavstämning.
Allt som rör `actual_cost_ore`, `kr/mil`
och `vehicle_battery_dc_kwh` förblir `null`.
Inga fysiska skrivningar eller nya styrbehörigheter
har introducerats.

## Manuellt lokalt verifieringsflöde (syntetiskt exempel)

Inmatning är avsiktligt **explicit** tills betrodda automationskällor finns.
Inga hemligheter, VIN eller kundidentifierare ska finnas i händelsefilen.
Skriv varje exempel i en separat lokal fil, och tillämpa dem i kronologisk
ordning (UTC med `Z` eller annan entydig offset).

Första händelsen, till exempel `opening.json`:

```json
{
  "event_id": "opening-20261028",
  "type": "opening",
  "occurred_at_utc": "2026-10-28T08:00:00Z",
  "quality": "estimated",
  "stored_dc_kwh": "30",
  "inventory_value_ore": null
}
```

Det null-värdet är **viktigt**: ett befintligt bilbatteri saknar normalt
känt historiskt anskaffningsvärde när loggningen påbörjas. Om det är
välgrundat kan `inventory_value_ore` i stället sättas till en särskilt
dokumenterad uppskattning, aldrig automatiskt till 0.

Exempel på en *hypotetisk* laddhändelse (`charge.json`), med blandad
köpt el och marginalt solöverskott, inte ett avläst fältvärde:

```json
{
  "event_id": "charge-20261028-1",
  "type": "charge",
  "occurred_at_utc": "2026-10-28T11:00:00Z",
  "quality": "estimated",
  "stored_dc_kwh": "18",
  "direct_aux_ac_kwh": "0",
  "tariff_version": "example-2026-10",
  "ac_sources": [
    {"source": "grid", "ac_kwh": "12", "price_ore_per_kwh": "150"},
    {"source": "solar", "ac_kwh": "8", "price_ore_per_kwh": "50"}
  ]
}
```

Körning, till exempel `drive.json`:

```json
{
  "event_id": "drive-20261028-1",
  "type": "drive",
  "occurred_at_utc": "2026-10-28T12:00:00Z",
  "quality": "estimated",
  "distance_km": "10",
  "consumed_dc_kwh": "2.2"
}
```

Anrop, med **egen tillfällig testdatabas** och utan fysiska anrop:

```bash
python3 scripts/services/ev_cost_ledger.py --db /tmp/ev-cost-test.sqlite ingest --file opening.json
python3 scripts/services/ev_cost_ledger.py --db /tmp/ev-cost-test.sqlite ingest --file charge.json
python3 scripts/services/ev_cost_ledger.py --db /tmp/ev-cost-test.sqlite ingest --file drive.json
python3 scripts/services/ev_cost_ledger.py --db /tmp/ev-cost-test.sqlite report
```

## Framtida anslutning till Home Assistant

1. Kalibrera levererad AC-energi från separat **EM111** om möjligt; tills
   dess är EVCC-strömintegral baserad på antagen spänning/PF en uppskattning.
   Logga per UTC-prisintervall och bevara råvärden + mätkvalitet.
2. Matcha verkliga nätköpspriser och nettoexportpriser med giltig tariff,
   och kontrollera solöverskott mot **husbatteriets prioriterade laddning**.
   Överför inte Solintegs skuggplans framtida prognos till historiskt
   faktiskt inköpspris. Flagga misstänkt EV-orsakad husbatteriurladdning.
3. Hämta körintervall från Volvo/HA:s odometer, trip-/SoC- och
   energivärden. Den befintliga XC40-dashboardens distansbinning
   `apps/home-assistant/ha-config/dashboards/volvo_xc40.yaml` i
   `ermhawi/ha-kube` använder ojämna rapporter och hoppar över
   laddningskorsande intervall; den måste återanvändas eller ersättas
   av en verifierad backendvariant innan körkostnader märks `metered`.
4. Kör kostnadsinsamling i en **egen isolerad read-only sidecar**.
   Skapa behörighetsstyrda kostnadshändelser och en privat JSON-rapport.
   Sätt `EV_COST_REPORT_PATH` och `EV_COST_READ_TOKEN` i Next.js
   **servermiljö**, aldrig i webbläsarens JS, HA-dashboardens YAML
   eller Git. Rapportfilen skapas med mode `0600`; ge endast
   den behöriga webbprocessen en explicit läsrätt via ACL/grupp,
   **inte** `chmod 644` på hela shadow-runtime.
5. Låt HA server-side REST-sensor eller särskild lokal integration
   läsa API:t med en hemlig Bearer-token; visa `kr/mil`, `kr/resa`
   och månadsöversikt med datakvalitet. Ingen ny dashboard-entitet
   eller laddstyrning är driftsatt av denna kod.

**Säker drift:** Ingen förändring av `/opt/solinteg/app`,
`solinteg-dispatch`, `solinteg-poller` eller EVCC:s 6 A-börvärde.
Det krävs separat godkänd driftsättning av *read-only*-kostnadssidan
och separata säkerhetsprov före eventuell fysisk EV-styrning.
