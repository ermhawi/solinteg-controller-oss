# EVCC: två laddlägen, pris och solöverskott (skuggläge, 2026-10-28)

**Status 2026-10-28:** isolerade HA-, Waveshare- och prisobservatörer är nu driftsatta på Dell. Den nya Next.js-sidan och fysisk bil-/inverterstyrning är fortfarande **inte driftsatta**; inga enheter har fått skrivkommandon.
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
- `scripts/services/ha_ev_session.py`: ett skrivskyddat HA GET mot
  `sensor.volvo_charge_priority_volvo_energy_snapshot` (schema v2).
  Separata tider visar när Volvo Energy State senast hämtades och när
  enskilda fordonsvärden senast **ändrades**. Det senare är inte en
  verifierad fysisk BMS-mättid. Den strikta EV-styrsessionen förblir
  ogiltig och en separat skuggsession kan bara skapas med uttryckliga
  och tydligt märkta energiantaganden.
- `scripts/services/evcc_observer.py`: en självständig observerare med
  en transport som **enbart konstruerar Modbus FC03**, för två kända
  registerfönster. Rapporterad kanalström är inte L3:s RMS-skydd.
- `scripts/services/ev_shadow_planner.py`: läser skuggsessionen,
  Solintegs prisplan, invertertelemetri och EVCC-observation.
  Föreslagen ström är endast en simulering; fysisk tillåtelse är 0 A.
- `app/ev/page.tsx`, `app/components/EvChargingPlanner.tsx` och
  `app/api/ev/*`: Solintegs separata mobilanpassade
  `/solinteg/ev`-sida (vid basväg `/solinteg`) med två laddlägen,
  80 %-mål, avresetid/pristak och read-only sessions-/skuggstatus.
  Sparning av **enbart skugginställningar** kräver
  `EV_SHADOW_SETTINGS_WRITES_ENABLED=1` **och** en serverkonfigurerad
  hemlig nyckel på minst 32 tecken, kontrollerad i konstant tid.
  Utan dessa går lägena att förhandsvisa men inte spara.
  Hemligheten exponeras aldrig från servern till webbsidan eller källkoden.
- `deploy/solinteg-ev-ha.service`, `deploy/solinteg-ev-shadow.service`
  och `deploy/solinteg-ev-observer.service` körs nu separat, som
  `solinteg-evshadow`, från `/opt/solinteg/ev-shadow-runtime`.
  Den äldre `solinteg-evcc.service` med manuella skrivmöjligheter
  är varken installerad eller aktiverad. Se rollout-dokumentet.

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
fälttider i `sensor.volvo_charge_priority_volvo_energy_snapshot`.
Den strikta kontrollsessionen avvisar gamla, saknade eller tvetydiga
sådana även fortsättningsvis; separata **preliminära skuggscenarier**
kan däremot använda lyckade API-observationer. **Inga HA-, Volvo-,
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
att den nya hämtningen misslyckats. **Den första diagnostikversionen**
gav `unavailable` vid så gamla fälttider. Version 2 gör i stället
skillnad mellan `observed` för preliminära förslag och
en fortsatt spärrad, separat kontrollsession.

**Implementerat i version 2:** en tvådelad kvalitetsmodell med
`api_received_at_utc` (ny lyckad energihämtning) och fältspecifika
`*_oem_at_utc` (senast ändrat). Den kan användas för tydligt märkta
**skuggförslag** men är inte ett verifierat fysiskt BMS-prov.
Så länge källans fysiska mättid inte kan fastställas, oberoende
L3-skydd saknas och EVCC-paus/readback inte verifierats är fysisk
EV-styrning avstängd.

## Separat observationsnivå för skuggläget (2026-10-28)

**Version 2** av den egna HA-sensorn publicerar två oberoende tidsfält:

- `api_received_at_utc` anger när den befintliga Volvo-koordinatorn
  **fick ett nytt lyckat Energy State-resultat**. Initial läsning av
  `coordinator.data` utan en ny callback ger inte någon bekräftad
  hämtning. Ett nytt resultat måste ha samtliga tre energifält.
- `battery_oem_at_utc`, `connection_oem_at_utc` och
  `target_oem_at_utc` betyder **senast ändrat i Volvo-API:t**,
  inte en verifierad tidpunkt när bilens BMS mätte värdet.
  En oförändrad målgräns kan med rätta ha en gammal tid.

HA-sensorn använder `ready` när alla fält ändrats nyligen,
`observed` när ett nytt giltigt API-svar innehåller fält vars
senaste ändring är gammal, och `unavailable` om API-resultatet
saknas eller har ogiltiga värden. Samtliga nivåer har
`advisory_only=true` och `physical_control_eligible=false`.

**Isoleringen mellan prisförslag och styrning är avsiktlig:**

- `EV_SESSION_PATH` innehåller **alltid en ogiltig styrsession**.
  Ingen ny API-observation, även med nyligen ändrade värden, gör
  det möjligt att använda den som källa för automatisk EVCC-styrning.
- `EV_SHADOW_SESSION_PATH` är en **separat typ** med
  `source=home_assistant_shadow`,
  `observation_time_semantics=api_fetch_not_bms_sample` och
  `energy_estimate_unverified=true`. Den kan bara skapas vid
  färskt (max 5 min) bekräftat API-resultat, anslutningsstatus,
  exakt lokalt 80 %-mål, och uttryckligt val
  `EV_SHADOW_ASSUMPTIONS_ENABLED=1` tillsammans med angivna
  antaganden `EV_SHADOW_ASSUMED_USABLE_KWH` och
  `EV_SHADOW_ASSUMED_AC_EFFICIENCY`.
  Inga antaganden fylls i automatiskt.
- `scripts/services/ev_shadow_planner.py` läser **endast**
  shadow-sessionen, husets befintliga `optimizer_runs` och färsk
  invertertelemetri (read-only). Den kan köra båda laddlägena utan
  Waveshare/EVCC. Med `EV_SHADOW_PLANNER_ENABLED=0` (default)
  arbetar den inte alls. Dess `charging_permitted_a` och
  `solar_permitted_a` är **alltid 0**, oavsett förslag.
- Gränssnittet kan läsa `EV_SHADOW_PLAN_PATH` och visar då
  både källtiderna, att energibehovet är hypotetiskt och att
  den föreslagna strömmen inte är ett utfört laddkommando.

**Live skuggprov 2026-10-28:** efter att HA-snapshot v2
(`ermhawi/ha-kube` `main`, commit `30a4086`) blivit frisk
(`Synced/Healthy`) gav den `state=observed`, nytt bekräftat
Energy State-resultat och en SoC-observation 63 %. Den äldre
`updatedAt=2026-10-28T12:05:06Z` behölls som **senaste ändring**.
Ett engångsprov mot **riktiga** `optimizer_runs` och invertermätvärden
använde endast temporära, privata filer och ett uttryckligt **okontrollerat
testantagande** om 81,608 kWh användbar kapacitet och 90 % AC-verkningsgrad.
Det gav 15,415 kWh *illustrativt* AC-behov till 80 %, köppris
119,47 öre/kWh och ekonomitröskel 102,75 öre/kWh. Båda
laddlägena föreslog vid provtillfället **0 A**, med motiven
`deadline: cheaper energy planned later` respektive
`economy: solar opportunity cost below threshold`. Den sistnämnda
producerade **inte** ett solstartsförslag eftersom ingen tillräcklig
färsk/stabil solel var tillgänglig i beslutsvillkoren.
I båda fallen var den strikta kontrollsessionen ogiltig,
`charging_permitted_a=0` och `writes=0`. Ingenting driftsattes
i Solintegs befintliga husbatteritjänst.

Det nya gränssnittet verifierades separat i mobil Playwright med
syntetiska statusfiler: det visar API-hämtning och senast ändrat-värde
separat, gör tydligt att energin är hypotetisk och skiljer på osparade
lägesändringar och den senast sparade skuggplanen. Previewservern
stoppades efter provet.

**Kvar före fysisk styrning:** verifierade OEM-sampletider (inte bara
uppdaterad API-cache), kapacitet/AC-kalibrering, last/effektbeslut
som inte laddar från husbatteriet, verifierad paus/readback via
Waveshare och oberoende L3-RMS-säkerhet. Inga dessa krav får
kringgås av ett framgångsrikt skuggtest.

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

**Länken återställd 2026-10-28:** Waveshare svarar nu från Dell på
`192.168.0.20:4196`. En separat FC03-observatör läser `0x000F` och
`0x002E` var 30:e sekund och rapporterar EVCC-status `B2`,
`configured_pwm_raw=267`, `pilot_pwm_raw=267` och cirka 0,3 A kanalström.
Pilotens inställning motsvarar cirka 16 A **tillgänglig** ström, inte
bevis för att bilen laddar med 16 A. Inga EVCC-skrivningar har utförts.
Upstream-timeout var tidigare avstängd och oberoende skydd för 20 A på
L3 saknas. Både `charging_permitted_a` och `solar_permitted_a` är 0 A.

Vid återupptag: verifiera länken, status, paus `0x03E8`, laddström 0 A,
återstart 6 A, avbruten kommunikation och fysisk säkerhetsfunktion.
Dokumentera resultat före beslut om begränsad pilot.
