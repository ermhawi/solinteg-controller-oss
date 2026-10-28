# XC40 energikostnad per mil – read-only bokföring (utvecklingsläge, 2026-10-28)

Se även designspecifikationens avsnitt 8.5 i
`docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md`.

## Status och gränser

Koden är **implementerad och testbar lokalt men inte driftsatt**. Ingen aktiv
kostnadspoller, ingen automatisk registrering från Volvo/HA/Tibber/EM111 och
ingen liveändring i laddbox, Volvo, växelriktare eller Solintegs dispatch.
Dagens `ev_history.py` har 30-sekundersobservationsdata, men saknar
verifierade kWh från EM111, exakt bilbatteri-DC-ökning och tillförlitligt
tidsmatchade körintervall. Därför är en tom kostnadsbokföring **inte**
en uppgift om `0 kr/mil`. Låt befintlig XC40-graf vara oförändrad tills
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

## Nästa read-only källadapter: Volvo-körjournalens verkliga intervall

En separat adapter i `scripts/services/ev_cost_capture.py` kan läsa
`GET /api/volvo_trip_energy/cost_segments` från den befintliga
`volvo_trip_energy`-integrationens **planerade, skyddade** HA-rutt.
HA-repot har motsvarande nya vy i utvecklingsutcheckningen, **ej
deployad**. Tills HA-konfigurationen har släppts via ordinarie GitOps
kommer URL:en inte att finnas i den körande HA-instansen.

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

Ingen EV-kostnadstjänst, HA-restart, GitOps-push eller ny automatisk
fysisk kontroll är aktiverad av denna kod. Nästa driftgrind är en
begränsad HA-endpointdeployeringsgranskning (inklusive HA-checksumma,
API-autentisering och rollback), följt av en läs-/insamlingspilot.

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

För framtida drift finns **ej installerade/ej aktiverade** systemd units
`solinteg-ev-cost-capture.{service,timer}` (var 30:e minut efter
godkänt HA-API) och
`solinteg-ev-cost-price-capture.{service,timer}` (en gång per dygn).
De två tjänsterna använder endast isolerad miljö, privata databaser och
nätbegränsad eller helt nätförbjuden read-only inhämtning.
Lokala source-filer för respektive collector kopierades till den
befintliga isolerade runtime som `root:solinteg` mode `0440` och
SHA256-jämfördes; det installerar/aktiverar **inte** systemd-enheter.

**Kvarvarande GO-grind:** Ingen laddningssession kunde verifieras i
EVCC-historiken (endast A1-vila eller misslyckade FC03-läsningar).
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
