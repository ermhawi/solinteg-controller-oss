# EVCC: två laddlägen, pris och solöverskott (skuggläge, 2026-10-28)

**Status: kod och offlinetester klara, INTE driftsatt eller hårdvaruverifierad.**
Följ etappgränserna i `docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md`.

## Kod

- `scripts/services/evcc_ascii.py`: en enhet, Modbus ASCII över transparent TCP,
  FC03 `0x000F` och `0x002E`, LRC, kontroll av svar och begränsat läs-retry.
  En enda FC10 till `0x0014` endast efter separata manuella spärrar, exakt
  förväntat råbörvärde och efterföljande läsback. Paus är `0x03E8` (100 %
  PWM, ingen laddström), **inte** 0 A. Start/återupptagning kräver en extra
  manuell startsignal. Ingen automatisk upprepning efter tvetydigt ACK.
- `scripts/services/ev_solar_policy.py`: rent skuggbeslut med separata
  bidrag från solel och pris/tidsplanerad nätel. Solöverskott får användas
  först vid minst 99,5 % SoC i husbatteriet (99 % vid pågående laddning)
  och tre stabila observationer. Planerad **billig nätel eller nödvändig
  deadlinesladdning** kan däremot föreslås även innan huset nått 100 %.
  Husbatteriets urladdning >150 W, gamla mätvärden och saknat eller
  uppnått bilbatterimål stoppar ändå varje bil-laddningsförslag.
  Överskottet räknas med `grid_w + EVCC-uppmätt EV-effekt - 200 W`
  så att bilen inte skapar sin egen start/stopp-pendling.
  Det befintliga 150 W-skyddet är fortfarande för konservativt när
  batteriet legitimt försörjer husets grundlast: ett EV-medvetet
  batteriskydd måste ersätta det före automatisk drift.
- `scripts/services/ev_price_policy.py`: två laddlägen som båda läser
  husbatteriets redan publicerade `optimizer_runs.inputs_json` (`buyPrice`
  med skatt, moms, nätavgift och påslag, samt `sellPrice` för export).
  **`deadline`:** prioritera 80 % vid avresa, fördela energibehovet på
  billigaste möjliga kvartarna; om tidsmarginalen tar slut rekommenderas
  laddning oavsett köppris, även vid prisbortfall. **`economy`:** ingen
  avresetid, använd bara relativt billiga elpriser – lägsta 25 % av upp
  till 48 h framtida köppriser och strikt under medianen när medianen är
  positiv. Valfri **absolut prisgräns** `max_buy_ore_per_kwh` ger användaren
  kontroll vid genomgående höga elpriser. Värdera sol med utebliven
  exportintäkt; ladda aldrig med solel som husets plan avsatt till
  husbatteriet. Båda stoppar laddningsförslag vid bilens 80 %-mål.
  Prisprognosernas riskpåslag återanvänder
  `SOLINTEG_FORECAST_RISK_PREMIUM_ORE`. Vid okända eller tvetydiga
  priser avstår `economy` från nya laddstarter.
- `scripts/services/evcc_service.py`: `--watch` sparar endast skuggbeslut
  i separat `evcc-shadow.json`, inklusive `charge_mode`,
  `target_soc_pct`, `departure_at_utc` (null i `economy`),
  `remaining_ev_ac_kwh`, `price_buy_ore_kwh`, `price_sell_ore_kwh`,
  `price_cheap_threshold_ore_kwh`, `grid_topup_suggested_a` och `price_reason`.
  Ingen FC10 på den vägen. `EV_ENABLED=0` (default) betyder **ingen
  EVCC-anslutning**, inga skrivningar och inga effekter på `optimizer_runs`
  eller befintlig batteridispatch.
- `scripts/services/ha_ev_session.py`: **ett** read-only HA REST GET mot
  `sensor.volvo_charge_priority_volvo_energy_snapshot`, som publiceras
  av vår befintliga HA-integration vid uppdatering av Volvos API-data.
  Snapshoten innehåller batteri-SoC, laddkabelstatus och bilens lokala 80 %-mål,
  **med Volvos fältspecifika ursprungstidsstämplar**. Kräver
  `schema_version=1`, `timestamp_origin=volvo_energy_state_field`,
  fullständiga färska (max 20 min) OEM-tider och högst 5 min gammal
  faktisk API-uppdatering. Varken senaste HA GET, HA `last_reported`
  eller koordinatorsensorns nyare tidstämpel är tillåten fallback. Kräver explicit
  kalibrerad **användbar** kapacitet och AC-verkningsgrad; HA:s
  `sensor.volvo_xc40_battery_capacity` (tidigare 81,608 kWh) används inte
  automatiskt som verifierad användbar kapacitet. Sätter
  `observed_at_utc` till **äldsta** källtidsstämpel; vid fel
  ogiltigförklaras tidigare giltig sessionsfil **omedelbart**.
- `app/ev/page.tsx`, `app/components/EvChargingPlanner.tsx` och
  `app/api/ev/*`: Solintegs separata mobilanpassade
  `/solinteg/ev`-sida (vid basväg `/solinteg`) med två laddlägen,
  80 %-mål, avresetid/pristak och read-only sessions-/skuggstatus.
  Sparning av **enbart skugginställningar** kräver
  `EV_SHADOW_SETTINGS_WRITES_ENABLED=1` **och** en serverkonfigurerad
  hemlig nyckel på minst 32 tecken, kontrollerad i konstant tid.
  Utan dessa går lägena att förhandsvisa men inte spara.
  Hemligheten exponeras aldrig från servern till webbsidan eller källkoden.
- `deploy/solinteg-ev-ha.service` och
  `deploy/solinteg-evcc.service`: **inaktiva** systemd-enheter;
  ingen automatisk installation, start, deploy eller ändring
  av skarp Solinteg.

## Delat elpris, men ännu inte gemensam verkställande plan

Prisbesluten kommer från samma optimerarkörning som husbatteriet:
`buyPrice = (spot + leverantörspåslag) × (1 + moms) + skatt/överföring` och
`sellPrice = faktiskt exportvärde`. Till skillnad från husbatteriet kan
Volvon inte sälja tillbaka den köpta elen. De två laddlägena uttrycker
**olika mål** med **samma ekonomi och säkerhet**:

1. **`deadline` / Klar till avresa:** 80 % vid fast UTC-avresetid är
   förstahandsmålet. Välj billigaste möjliga tidsfönster men använd även
   dyr el när det annars inte går att hinna. För kort laddfönster eller
   otillräcklig fasbudget ska visas som ett olösbart mål, inte påstås vara
   en garanti. Om prisinformationen saknas kan enbart tidskritisk laddning
   föreslås; felaktiga/tvetydiga snapshots används aldrig.
2. **`economy` / Ladda ekonomiskt:** **ingen avresetid** och inget
   åtagande om när bilen blir klar. Ladda när dagens uppmätta importpris
   är inom relativt billigaste kvartilen av tillgänglig 48 h-prognos,
   eller när det faktiska exportvärdet av solelen är lika lågt. En
   valfri, explicit totalpristaksgräns i öre/kWh kan vara striktare.
   Relativt billig är **inte** alltid billigt i absoluta kronor; ett
   användarvalt tak behövs om det är viktigt. Om prisunderlaget inte
   räcker för minst 6 h jämförelse avstår läget från laddstart.
   Målet är fortfarande 80 %, därefter stannar alla förslag.

**Begränsad första leverans:** laddplanen är en marginalkostnadsordning
med *husets publicerade plan fixerad*. Den är **inte** ännu en gemensam
tvålagersoptimering som omplanerar hus- och bilbatterierna ihop. Framtida
samordnad planering måste ta hänsyn till att bilens last förändrar husets
batteriplan och dess intäkter; den får inte aktiveras bara för att denna
skuggberäkning klarar offlinetester.

Före laddningsförslag krävs en **separat producerad** färsk
`EV_SESSION_PATH`-JSON från en betrodd read-only HA-läsare:

```json
{
  "source": "home_assistant",
  "connected": true,
  "observed_at_utc": "2026-10-28T10:00:00Z",
  "charge_mode": "economy",
  "target_soc_pct": 80,
  "vehicle_soc_pct": 67,
  "remaining_ac_kwh": 10.2,
  "max_buy_ore_per_kwh": 120
}
```

Exemplet är **schemaillustration, inte en mätning eller rekommenderat
pristak**. I `deadline` byts `charge_mode` och man lägger till en
fast `departure_at_utc` i UTC. I `economy` finns **ingen avresetid**,
och `max_buy_ore_per_kwh` är **valfritt**. Oavsett läge ska
`remaining_ac_kwh` härledas från verifierad användbar batterikapacitet,
laddförluster och färska fältspecifika **Volvo Energy State-tidsstämplar**;
`observed_at_utc` är äldsta verifierade OEM-`timestamp`, aldrig tidpunkten
för HA:s senaste poll eller Solintegs senaste GET. Även Volvos `timestamp`
kan avse när ett fält uppdaterades snarare än när BMS fysiskt mätte det.
Sätt aldrig tiden till nu om källvärdet är gammalt.

**Läsaren och UI-valet finns nu som separat utvecklingskod men är
inte driftsatta.** Utan en färsk, validerad HA-session blir både
nätandelen och det slutliga laddförslaget 0 A.
Det här är endast ett stopp i **planeringens förslag** vid 80 %, inte
en fysisk stoppfunktion. För skarp drift måste bilens lokala
80 %-gräns och EVCC:s paus/readback verifieras oberoende; HA:s
molnfördröjning räcker inte som säker sista stoppåtgärd.
`EV_SESSION_PATH` ska inte fyllas med gissad SoC eller nollställas vid
varje poll. Unika UTC-tider behövs vid sommartidsbyte; gamla
tvetydiga 02:xx-kvartar ger inget nätladdningsbeslut.

`EVCC_MAX_CURRENT_A` är högst 6 som standard tills fas- och EVCC-skydd
verifierats. `solar_permitted_a` förblir 0 eftersom oberoende L3-skydd
inte är verifierat. Befintliga säkerhetsspärrar, batterirecovery och
stopp vid för gammal data har företräde framför båda energikällorna.

## Säker HA-integration och två användarval

1. Använd befintlig Home Assistant API med en **separat skyddad tokenfil**
   som endast HA-läsaren får läsa. Ingen token, VIN eller lösenfras får
   committas. De befintliga hemliga HA-värdena i
   `ha-kube/.env` lästes enbart i minnet vid utvecklingsprovet.
   Den gamla `HA_URL` pekade på ett certifikat med fel värdnamn;
   **TLS-kontroll får inte inaktiveras**. I testet nåddes HA i stället
   via dess privata Kubernetes ClusterIP med HTTP, inte via en offentlig
   osäker transport. Slå upp aktuell ClusterIP i stället för att
   förutsätta att testets adress alltid gäller.
2. Ange `HA_API_URL`, `HA_READONLY_TOKEN_FILE`,
   `EV_STATUS_PATH`, `EV_SESSION_PATH`, `EV_SETTINGS_PATH`,
   och de **verifierade** parametrarna
   `EV_USABLE_BATTERY_KWH` och `EV_AC_CHARGE_EFFICIENCY`.
   Om någon kapacitetsparameter saknas blir sessionen ogiltig.
   Exempel finns i `deploy/solinteg.env.example`.
3. Den nya läsaren har egna tester, inklusive avvisande av gammal
   SoC trots att Volvos laddstatus eller HA-koordinator uppdaterats
   senare, tidsskillnaden vid svensk sommar-/vintertid och omedelbar
   ogiltigförklaring av en tidigare giltig session vid databortfall.
   `volvo_charge_priority` publicerar bara en komplett
   `volvo_energy_snapshot` om **alla tre fält** har egna OEM-tider.
   Solinteg-förbindelsen accepterar inte de äldre HA-sensorernas
   `last_reported` som fallback. Fortsatt måste vi verifiera Volvos
   faktiska timestamp-semantik under laddning innan fysisk styrning.
4. När webben driftsätts visas kortet via `/solinteg/ev`.
   Välj `Klar till avresa` eller `Ladda ekonomiskt`, med fast 80 %-mål.
   **Att välja ett radiokort gör inget i bilen.** Om inställnings-API:t
   ännu är låst kan valet bara förhandsvisas. Separat explicit
   aktivering och en lång lokal skrivnyckel krävs för att
   spara den atomiska konfigurationsfilen.
5. **Ingen automatisk styrning är klar**: EVCC-länken,
   20 A L3-fasskydd, batteriets faktiska marginalurladdning och
   oberoende paus/återstart återstår.

**Tidigare live read-only-resultat 2026-10-28 (före OEM-förbättringen):**
HA:s batterisensor hade flera timmar gammal `last_reported` medan
laddstatus uppdaterades oftare. Den tidigare Solinteg-läsaren gav därför
`valid_for_planning=false`. Nya läsaren kräver Volvos ursprungliga
fälttider i `sensor.volvo_charge_priority_volvo_energy_snapshot` och
avvisar gamla, saknade eller tvetydiga sådana. **Inga HA-, Volvo-,
EVCC- eller inverterskrivningar utfördes i diagnostiken.**

## Fälttidsstämplar verifierade mot live Volvo 2026-10-28

En fristående, read-only HA-diagnos har nu driftsatts från
`ermhawi/ha-kube` på `main`, commits `2a27e9b` och `3bd4b30`.
Home Assistant rapporterar `Synced/Healthy`. Den officiella
`volvocarsapi`-parsern flyttar JSON-fältet `updatedAt` till
`extra_data.updated_at`; därför var `field.timestamp` tidigare
`None`. Vår HA-kod hämtar nu det faktiska `updated_at`-värdet
utan att ändra upstream-koden eller göra nya fordonsanrop.

Ett verkligt snapshot visade vid cirka 12:51 UTC:
- `batteryChargeLevel`: `updatedAt=2026-10-28T12:05:06Z`, 63 %.
- `chargerConnectionStatus`: samma `updatedAt`, ansluten.
- `targetBatteryChargeLevel`: `updatedAt=2026-10-18T10:04:37Z`, 80 %.
- Ny HA-uppdatering med `api_received_at_utc` omkring 12:51 UTC.

**Viktig datatolkning:** `updatedAt` kan avse **senast ändrat värde**,
inte senast mätt. Att laddmålet är oförändrat i tio dagar betyder inte
att den nya hämtningen misslyckats. Den nuvarande HA-snapshoten är
medvetet `unavailable` vid så gamla fälttider, och Solinteg-läsaren
avvisar den. Det är en säker men för konservativ policy för ett
framtida skarpt laddschema.

**Nästa nödvändiga förbättring:** en tvådelad kvalitetsmodell med
`api_received_at_utc` (faktiskt färsk energihämtning) och
`field_updated_at_utc` (senaste ändring). Detta kan användas för
markerade **skuggförslag**, men får **aldrig** räknas som verifierat
fysiskt SoC-prov utan oberoende stöd från Volvo/EVCC. Så länge
källans mättid inte kan fastställas, oberoende L3-skydd saknas och
EVCC-paus/readback inte verifierats är fysisk EV-styrning avstängd.

## Statuskontroll utan skrivning

Från utvecklingsutcheckningen, med `EV_ENABLED=0`:

```bash
python3 scripts/services/evcc_service.py
python3 -m unittest discover -s scripts/tests -p 'test_ev*.py' -q
```

Om Waveshare är åtkomlig och **ensam EVCC-länkägare** kan en read-only
statushämtning göras med enbart `EV_ENABLED=1` och `EVCC_HOST` satt till
den verifierade gatewayen (port 4196). `EVCC_CONTROL_ARMED` lämnas 0.

## Fysiskt prov: inte godkänt av kodtest

En eventuell manuell paus/återupptagning kräver att en installatör eller
operatör först verifierar 20 A-fasens RMS-ström, bilens 6 A-tak, aktuell
Modbus-status, och har en oberoende väg att stoppa laddningen. Före ett
separat tidsbegränsat prov måste även aktuellt EVCC-råbörvärde sparas.

Manuella CLI-kommandon kräver **alla** följande:
`EV_ENABLED=1`, `EVCC_CONTROL_ARMED=1`, `EVCC_MANUAL_TEST=1`,
`--confirm`, `--expected-raw`. Återupptagning kräver dessutom
`EVCC_START_APPROVED=1`. Dessa värden får inte sättas i normal
systemd-drift. En bekräftad FC10-kvittens är inte en verifierad fysisk
paus. Vid utebliven kvittens behandlas status som **okänd**; ingen
automatisk retry eller återstart görs.

**Hårdvarublockering 2026-10-28:** Dell rapporterade `No route to host`
mot den tidigare verifierade Waveshare-adressen/porten; ingen FC03 eller
FC10 kunde testas live. EVCC:s upstream-timeout har tidigare visats
vara avstängd. Utan ett verifierat *oberoende* stoppskydd/fasbudget får
ingen automatisk EVCC-styrning aktiveras; `solar_permitted_a` är alltid
0 i den aktuella skuggservicen. `solar_suggested_a` är **inte** en säker
styrsignal.

Vid återupptag: verifiera länken, status, paus `0x03E8`, laddström 0 A,
återstart 6 A, avbruten kommunikation och fysisk säkerhetsfunktion.
Dokumentera resultat före beslut om begränsad pilot.
