# ADR 2026-11-04: Förbrukningsprognos per dagtyp, EVCC-last separat

**Status:** Driftsatt och verifierad på Dell 2026-11-04.
Gäller endast branch `Sundsvall` (aldrig `main`).

## Observerad avvikelse

Den 4 november visade planen för torsdag 5 november, kl. 10,
hushållslast **2 084 W reserverad** mot **1 680 W prognostiserad PV**.
Batteriet förutsågs falla till ca **13,4 %** trots att novemberdagens
solkurva ökade. Detta var inte ett bevis för otillräcklig solenergi.

Tidigare `readTrailingLoadProfile` grupperade 14 dagars timmedelvärden
efter lokal klocktimme utan att skilja vardagar och helger, och lade
80:e percentilen över alla dagarna. Vid kl. 10 hade söndag 25 oktober
**3 883 W** och lördag 31 oktober **3 459 W**, mot torsdagarna
22 oktober **634 W** och 29 oktober **849 W**. Den sammanslagna
Q80 blev **2 084 W**. Vardags-Q80 blev **1 128 W**.
Den gamla modellen kunde dessutom räkna hela bilen som hushållslast.

## Ny lastmodell

- All fysisk `readings.house_load_w`, `grid_w`, L1–L3-CT och
  säkringsövervakning lämnas **byte för byte oförändrad**.
- `lib/telemetry/readings.ts` tar fortfarande timvis
  SQLite-aggregat. Nu beräknas därutöver vardags- och helgmedelvärden
  samt separat 80:e percentil per klocktimme. Dagtyp väljs utifrån
  slotens **lokala kalenderdatum** och är oberoende av UTC/DST.
- Vardagskurva används bara när minst fem vardagar finns och varje
  klocktimme har data; klassad Q80 kräver minst sju dagar.
  Helgkurva används vid minst tre helgdagar med full timtäckning;
  klassad Q80 kräver minst tre dagar (konservativ när n=3–4).
  Vid ofullständig grupp faller endast den gruppens prognos tillbaka
  till befintlig sammanslagen kurva. Vid saknad mätprofil används
  tidigare temperaturjusterade statiska modell.
- Den normala temperaturjusteringen och grundläggande
  `LOAD_FORECAST_MARGIN` **1,15** är oförändrade.
  Timmedelvärde används som förväntad förbrukning, gruppens Q80 som
  separat reserv. `optimizeDispatch`, terminalvärde, slitagemodell
  och husbatteriets fysiska reglering ändras inte.
- Cachen för historisk last är fortsatt dagsbaserad, men cache-nyckeln
  byts när en ny EVCC-bekräftad UTC-timme läggs till. En saknad
  export gör att tidigare profil utan bilsubtraktion används.

## Mätt bil, inte estimerad ur inverter

Befintliga `solinteg-ev-history.service` läser
`ev-history.sqlite` med mode **0600** och producerar dessutom
`ev-load-hourly.json` i samma grupp-läsbara runtimekatalog (mode
**0640**, atomisk publicering, ägare `solinteg-evshadow`).
Webbprocessen har ingen direkt tillgång till privata EV-databasen.

Per avslutad **UTC-timme** accepteras ett EVCC-mätvärde bara om
samtliga villkor är uppfyllda:

1. FC03-data läst framgångsrikt och färskt, fysisk ström från
   `Ict2`, status A1/B2/C2 och sammanhängande en-fas-verifikation.
2. Minst **100 distinkta** EVCC-tidsstämplar, minst **50 minuters**
   mätspann och minst **85 %** godkända observationer.
3. Uppmätt Ict2 mellan 0–16,5 A, övriga faser högst 0,5 A;
   faktisk laddning förutsätter C2 och `charging_detected`.
4. Sanerad effekt per timme blir medelvärdet av **Ict2 × nominellt
   230 V** för laddande prover, 0 W när pålitlig EVCC-status bekräftar
   att ingen laddning sker. Det är en mätbaserad effektuppskattning,
   **inte** separat aktiv-effektmätning i W.
5. Om mätt bildeffekt överstiger samma timmes uppmätta
   totalförbrukning med över 150 W sker **ingen subtraktion**.

Ingen extrapolering, SoC-gissning, interpolation eller antagande om
0 A används för luckor. Endast accepterade timmar subtraheras
**från den härledda timprofilen** innan medel/Q80 räknas.
Timmar utan EVCC-täckning behåller rå last. Publicerad JSON
innehåller bara UTC-timnyckel, nominellt `evAvgW`, antal prover
och proveniens; inga bil-ID, rörelser, privata telemetrirader
eller token.

## Batteriets 20 %-reserv

**20 %** är ekonomisk planeringsreserv, **12 %** separat operativ
mjukvarugräns. När start-SoC redan är under 20 % modelleras
det tidigare uppmätta EMS-batterisparuttaget cirka **220 W**
(och cirka 276 W per 15-minuters DP-steg efter avrundning).
Därför får prognosen i vissa tider sjunka ned mot
cirka **13,4 %** tills kostnadsstyrd återhämtning inträffar.
Detta får inte felaktigt beskrivas som aktiv arbitrage-urladdning
under 20 %. Ingen ny EMS-skrivning, hårdvarugräns, BMS-inställning
eller laddpolicy ändras i denna ADR. Om en verifierad, korrekt
återspelning av den driftsatta optimeraren efter prognosändringen
fortfarande förutspår underskridande ska det hanteras som separat
drift-/prisväntansfråga; justera inte säkerhetsgolvet för att
kosmetiskt dölja problemet.

## Kvalitet och rollback

Kvalificering: Python-historikertester (kompletta, partiella,
inkonsistenta timmar), TypeScript-validator, integrationstest
vardag/helg inklusive EVCC-subtraktion och fallback till råhistorik,
hela kod-/dokumentationsbyggen. Den temporära fristående
optimerar-återspelningen med anpassat Node-instrument reproducerade
inte den redan loggade produktionsplanen. Därför får dess absoluta
SoC-värden **inte** användas som säker verifiering eller som skäl
att ändra 12/20 %-gränserna. Jämför hellre de riktiga
`optimizer_runs` före/efter förändringen, med färsk SoC och
väderprognos och redovisa osäkerheten.

Driftsätt endast dessa filer: `lib/load.ts`,
`lib/telemetry/readings.ts`, nya `lib/ev-load-hourly.ts`
och historian `scripts/services/ev_history.py` med separat
käll-/Next-backup och verifierade SHA-värden. Bygg en ny `.next`
som webbanvändaren `solinteg` för att undvika rotägd
`.next/trace`. Återställ fil och tidigare `.next` atomiskt vid
fel, starta historikern och webben igen; övriga tjänster
(dispatcher, broker, poller, EVCC-observatör, ordinarie
watchdog) lämnas orörda. Ingen EVCC-prisstyrning installeras.

## Verifierad Dell-release 2026-11-04

- **Före:** optimizer_runs ID `12008`, skapad
  `2026-11-04T15:50:43Z`. Torsdag 5 november kl. 10:
  förbrukningsreserv **2 084 W**, planerad husbatteri-SoC
  **13,38 %**.
- **Efter:** optimizer_runs ID `12009`, skapad
  `2026-11-04T15:51:54Z` med normal `POST /api/replan`.
  Samma morgonklockslag: förbrukningsreserv **1 128 W**,
  planerad SoC **22,54 %**, solprognos **1 680 W**.
  Prognosen visar ca **29,42 % kl. 12:00**.
- **20 %-kontroll:** Minsta prognosen för **torsdag 5 november**
  blev **17,96 % kl. 01:00**, inte 13,4 % runt kl. 10.
  Den totala kvarvarande lägstanivån **13,38 %** inträffar först
  **fredag 6 november kl. 23:15**, mot slutet av horisonten,
  och avser fortfarande modellens restuttag i EMS-sparläge.
  Ingen ny generell 20 %-spärr infördes eftersom den skulle
  kräva separat fysisk och ekonomisk verifiering.
- **Driftskomponenter:** Endast befintliga `solinteg-web.service`
  och `solinteg-ev-history.service` startades om. En ny
  timsammanfattning av **5** fullständigt verifierade EVCC-timmar
  publicerades som `ev-load-hourly.json` mode 0640 och testades
  läsbar för webbprocessen. Privat `ev-history.sqlite` mode 0600.
  8 verifierade systemd-tjänster var aktiva efter releasen.
  EVCC-priscontroller service/timer fortsatte vara `not-found`.
- **Webb:** Next build-ID ändrades från
  `aF6E6oCYGEE8dqY_UOu2Z` till
  `6RCHpqYzh2SXkc3WKHJ7X`.
  `/solinteg`, `/solinteg/api/inverter` och
  `/solinteg/api/plan` returnerade HTTP 200.
  Inga förändringar gjordes i broker, dispatcher, Modbus,
  råtelemetri, skyddsgränser eller EVCC.
- **Test:** **1 463 Python-testfall** godkända,
  **738 Vitest godkända (ett överhoppat)**,
  TypeScript, ESLint, Zensical, Next-produktionsbygge och
  `git diff --check` godkända.
- **Rollback:** Rootägd och SHA-verifierad original- och
  kandidatkälla, tidigare Next-`.next` och releasemanifest
  finns under
  `/var/lib/solinteg-evshadow-rollback/load-profile-weekday-ev-20261104/`.
  `manifest.json` har `status=deployed_verified`.
  Tidigare `.next` finns intakt i `next-before`.

Observerad ny SoC är en **prognos**, inte bevis på verklig
solproduktion eller framtida uppladdning. Den senare kontrollen
måste jämföra faktisk SoC och PV mot denna prognos.
