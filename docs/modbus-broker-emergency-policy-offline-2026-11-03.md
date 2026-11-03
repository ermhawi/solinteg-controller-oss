# Offline: SoC-grind för privilegierad Modbus-nödfallback (2026-11-03)

**Status:** källkod och offline-felinjektionstester klara. **Ingen fysisk
fallback installerad eller aktiverad. Ingen Modbus-skrivning utförd.**
Dells befintliga `solinteg-broker-observer.timer` kör fortsatt enbart
observation; gamla direktpollern/dispatchern/watchdoggen är orörda.

## Utgångspunkt och beslut

`modbus_broker_host_integration.py` hade redan ett komplett *syntetiskt*
test av root-adapter, systemd-stoppsvar, process-/socket-/TCP-observation
och exklusivt `flock`. Det saknade en faktisk SoC-baserad, oberoende
prövning innan en simulerad `General/Auto` kunde anropas.

Denna etapp tillför `modbus_broker_emergency_policy.py` och gör följande
grind obligatorisk i `coordinated_emergency_auto()`:

1. **Separat säkerhetskälla måste vara konfigurerad.** Utan
   `read_independent_evidence` sker ingen marköråterkallelse, inget
   `systemctl stop`, ingen ägarskapsöverlämning och ingen skrivning.
2. För en konfigurerad källa sker marköråterkallelse och verifierad
   frånvaro, stopp av **endast** den godkända broker-enheten,
   icke-blockerande exklusivt kernel-`flock` och **två** separata
   operativsystemkontroller av brokerprocess, UDS och TCP:502.
3. Den externa evidenskällan får anropas **först efter fencing** och
   under det fortfarande hållna exklusiva låset. Samordnaren kräver en
   `GeneralFallbackEvidence`, inte `True` eller broker-`live.json`.
   Evidensen måste hävda verifierat oberoende post-fence-ursprung,
   tidszonsmedveten färsk tidsstämpel (högst 15 s),
   frisk BMS, verkställbar native SoC-gräns, känd driftmode
   och giltig procentuell SoC.
4. För att General ens ska vara **kandidat** måste verifierad native
   `OnGrid EndSOC` vara minst den efterfrågade reserverade SoC:n
   (prototyp: **20 %**) och aktuell SoC ha minst **2 procentenheters**
   marginal över både native-golv och reserv. Saknas detta blir
   utfallet `write_guard_denied`, **utan fysisk General**.
5. Kontrollera att brokerapproval fortfarande saknas; verifiera även
   `monotonic`-deadline **efter** evidensläsningen och omedelbart
   före eventuell `emergency_auto`. Det lägre
   `fenced_emergency_auto`-lagret vägrar nu ett saknat
   `before_write` med `write_guard_missing`.
6. Bara godkänd sammanlagd grind tillåter **ett enda** injicerat
   Auto-försök med verifierad readback. Misslyckat/okänt utfall
   flaggas; inga automatiska omskrivningar, ingen återaktivering
   av brokerapproval.

## Verifierad verklig begränsning

Dokumenterad installation har **native OnGrid EndSOC cirka 10 %**,
medan den ekonomiska reserven är **20 %**. Alltså måste
`review_general_fallback` svara
`native_floor_below_required_reserve`: **automatisk General är
inte godkänd av denna policy**, även vid högre aktuell SoC.
Det är avsiktligt; General kan annars fortsätta självförbruka
under programvarans önskade reserv.

Spärren kan däremot lämna ett redan aktivt `0x0303`-börvärde kvar.
Detta är **inte** en fullständig fysisk säkerhetslösning.
En verifierad säker neutralisering, BMS-/SoC-beteende och
realistisk fel-/rollbacksekvens måste fastställas innan
skarp drift. Inga native skyddsregister har ändrats.

**Viktigt:** `verified`, `native_floor_enforced` och
`source=independent_post_fence` är endast kontraktsfält.
Offline-testerna *injicerar* dem. Ingen privilegierad
oberoende mätkälla, autentisering, faktisk BMS-/native-golv-verifiering,
installationspolicy eller persistent incidentlatch har införts.
Att en fakedatapunkt passerar policyn är **inte** tillstånd att
skriva till växelriktaren.

## Tester och felmatris

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_modbus_broker_*.py'
python3 -B -m unittest discover -s scripts/tests -p 'test_*.py'
```

**Verifierat 2026-11-03:** 166 brokerinriktade tester och hela
Python-sviten **1 330 tester OK**. Frontend: **703 godkända, 1
överhoppat** (testtimeout 15 s), ESLint och TypeScript godkända.
Zensical-dokumentationsbygge och `git diff --check` godkända.

Tester täcker verifierad och saknad/stale/pre-fence SoC,
ogiltig procent eller mode, BMS-larm, native-golv under reserv,
spoofad bool, saknad eller kastande källa, konkurrerande
TCP-sessioner, aktiv broker/UDS, misslyckat systemd-stopp,
upptagen lock, återkommen approval, långsam säkerhetsbedömning,
timeout efter kontroll och saknad slutgrind. Allt med
syntetiska systemd-/`ss`-svar, `TemporaryDirectory` och
fake-kommando/readback. Det sker **inga anslutningar till fysisk
Modbus eller installerade systemd-tjänster** under testerna.

## Nästa grind före verklig aktivering

- Utveckla oberoende, autentiserad SoC-/BMS-/mode-evidens som
  inte använder gamla `live.json` eller konkurrerande TCP:502.
- Bestäm och verifiera vilken faktisk säker fallback som
  bevarar reserv eller stoppar kvarhängande EMS-urladdning.
- Verifiera att nätverks- och restart-fencing även stoppar
  andra och framtida Modbusklienter (lokal flock/ss räcker inte).
- Lägg till privilegierad systemd-runtime, persistent incidentlatch,
  larm och operativ rollback, **endast efter separat Go/No-Go**.
- Utför read-only-förkontroll och explicit godkända fysiska
  tester med dokumenterad faktisk readback; ingen automatisk
  brygga till skarp drift.

Relaterat: [brokerplan](plans/modbus-broker-2026-11-01.md),
[EMS kontra General](architecture/adr-2026-11-02-ems-general-failsafe.md),
[Dell-observatör](modbus-broker-watchdog-dell-2026-11-03.md).
