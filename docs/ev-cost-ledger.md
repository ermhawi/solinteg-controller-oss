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
