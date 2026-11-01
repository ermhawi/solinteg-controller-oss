# Tillfälligt förbikopplat batterisparläge – 2026-11-01

**Status:** Kod för ett separat, avgränsat fysiskt försök. Ändring i
databasplanen aktiveras **enbart** med explicit `--start --confirm`.
Kontrollera rotägda loggen för faktisk aktivering och resultat.

## Syfte

Ordinarie optimizer planerar ett `idle`-intervall med
`batterySaveDriftKwh > 0` och den vanliga dispatchern översätter detta
till cirka **50 W** urladdning. Nästa planerade urladdning börjar först
**21:45**, vilket kan göra att det oberoende 25 %-reservprovet hinner
avslutas innan huset kommit ned till återhämtningsgränsen.

Vi vill tillfälligt **använda batteriet till husets egen last** genom
att ta bort **enbart det aktuella 15-minutersintervallets**
`batterySaveDriftKwh` ur den senaste, fortfarande giltiga
`optimizer_runs.dispatch_json`. Den installerade dispatcherns
ordinarie tolkning av `idle` utan markören är `General/Auto`, vilket
gör att batteriet i stället täcker hushållets aktuella underskott.
Detta är **inte** en direkt Modbus-urladdning och säljer inte
avsiktligt el till nätet. Övriga planfält, priser, framtida intervall,
komponenter och den ursprungliga dispatcherkoden lämnas oförändrade.

## Avgränsning, säkerhet och återställning

- En separat rotägd timer granskar en ny aktuell optimizer-plan var
  cirka **15:e sekund**. Varje ny körning från appen (normalt var
  femte minut) kan ha skapat tillbaka sparmarkören; endast det då
  aktuella `idle`-intervallet justeras, förutsatt att den fortfarande
  innehåller sparmarkören. Vanliga redan planerade `charge`- och
  `discharge`-intervall ändras aldrig.
- **Stoppa förbikopplingen vid SoC <=26,5 %**, så att den redan aktiva
  processlokala 25 %-reservstyrningen prioriterar laddning till **28 %**.
  Ordinarie dispatcher gör fortfarande det fysiska inverterarbetet.
- Även om SoC inte når målet slutar provet **2026-11-01 kl. 21:45
  svensk tid**. En ytterligare *separat* kalenderbaserad systemd-timer
  anropar samma avbryt- och återställningsrutin vid detta klockslag,
  oberoende av den var-15:e-sekund-timern.
- Den aktiva oberoende SoC25-reservens rotstatus måste hela tiden
  visa `running` och en färsk watchdog-avläsning. Volvon måste vara
  verifierat urkopplad `A1` med **okända/null strömmar**, inte påstått
  mätvärde 0 A. Ansluts bilen igen eller försvinner bevis stoppar
  bypassens egen timer och 25 %-reservens egen watchdog kan återställa
  den installerade ARMED-dispatchern oberoende av detta test.
- Stale/obekräftad data, SoC utanför testfönstret, temperatur utanför
  5–45 °C, PV >100 W, okänd fasbalans eller mätimport >=3000 W/fas
  eller >=6300 W totalt stoppar den temporära ändringen. Dessa är
  konservativa samplade gränser, **inte** certifierat säkringsskydd.
- Före *varje* SQLite-uppdatering journalförs en exakt kopia av hela
  ursprungliga `dispatch_json` i rotägd fil (0600) och SHA-256 av den
  avsedda patchen. Vid avslut återställs bara rader vars plan fortfarande
  exakt matchar vår patch; oberoende nyare förändringar får inte skrivas
  över. Om återställningen misslyckas sparas ett `restore_blocked`
  tillstånd för säkert omförsök.
- Ändrar **inte** laddboxens inställningar, SoC-golv i inverter/BMS,
  `solinteg.env`, den installerade dispatcherns källkod eller det
  redan aktiva 48-reservprovet. Den sistnämnda har fortsatt eget
  klockslag **2026-11-02 01:07 CET** för absolut rollback.

## Kontroll, start, insyn och stopp

```bash
sudo -n python3 scripts/tools/start_temporary_soc25_plan_bypass.py --check
sudo -n python3 scripts/tools/start_temporary_soc25_plan_bypass.py --start --confirm
sudo -n python3 /usr/local/libexec/solinteg-soc25-plan-bypass/temporary_soc25_plan_bypass.py --status
sudo -n python3 /usr/local/libexec/solinteg-soc25-plan-bypass/temporary_soc25_plan_bypass.py --stop
systemctl list-timers --all | grep 'solinteg-soc25-plan-bypass'
```

Kontrollera resultatet fysiskt med `live.json` och dispatcherns
`control_actions`: en planpatch är **inte i sig bevis** för att
invertern har återgått till Auto eller att hushållet försörjs av
batteriet. Förväntat förlopp är först byte från 50W sparbörvärde till
Auto och ökad självförbrukning; när SoC sedan når 26% (SoC-avläsningen
är heltalsbaserad) ska 48-reservprovet börja ladda till 28% med
separat säkerhetsövervakning.

```bash
python3 -m unittest scripts.tests.test_temporary_soc25_plan_bypass -v
systemd-analyze verify deploy/solinteg-soc25-plan-bypass*
```

## Risker och begränsningar

Den optimerade planens `socAfter`-prognos kan under provet avvika
från verkligheten eftersom vi avsiktligt åsidosätter sparintervallets
fysiska tolkning. En ny plan loggas automatiskt från den nya uppmätta
SoC-nivån. Kostnaden under perioden kan bli något högre när lagrad
energi används tidigare än vad prognosen rekommenderade.

En körning kan stoppas tidigare vid tung hushållslast eller
säkerhetsavvikelse. Om bilen återansluts måste separat lastbalansering
och EV-prioritering hanteras innan någon nytt prov får starta.
