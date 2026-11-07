> **Arkiverad 2026-11-07:** Historiskt experiment eller migrationssteg, inte en aktuell driftinstruktion. Kör inte gamla installations-, test- eller rollbackkommandon utan ny verifiering. [Nuvarande drift](../../current-state.md) · [Arkivöversikt](../index.md).

# Tredje övervakade återhämtningsprovet, 25–26 → 28 % (2026-11-01)

**Status: tredje fysiska provet avbrutet av säkerhetswatchdog
2026-11-01 kl. 21:10 CET.** Återställning av originaldispatchern och
båda egna timers verifierad. En fysiskt uppmätt negativ batterieffekt
observerades en gång, men avläsningarna var sinsemellan inkonsekventa
så provet kunde inte bekräfta stabil laddning eller SoC 28 %.
Det finns **inget aktivt permanent 25 %-SoC-skydd**.

## Syfte och tidigare prov

Två tidigare, oberoende övervakade försök har avslutats:

- 48-provet avbröts kl. 20:38 CET vid första 26 %-mätningen,
  eftersom watchdoggen krävde laddläge innan nästa 30s dispatcher-tick.
- 49-provet avbröts kl. 20:57 CET på en inkonsekvent batteri/AC-avläsning
  under övergången till General/Auto, trots att CT/last/netto var
  konsekventa. Ursprunglig dispatcher återställdes och inga
  fysiska återhämtningsladdningar bekräftades.
- Tillhörande planförbikoppling är stoppad och **49/49** JSON-planer
  återställda, inga överhoppade. Detta försök återaktiverar inte
  planförbikopplingen.

Det tredje försöket har helt egna sökvägar och systemd-enheter:

- Providentitet: `soc25-recovery-third-20261101`
- Root-status: `/var/lib/solinteg-soc25-recovery-third/status.json`
- Root-kod: `/usr/local/libexec/solinteg-soc25-recovery-third/`
- Tillägg: `50-soc25-recovery-third.conf`
- Timers: `solinteg-soc25-recovery-third-watchdog.timer` (cirka 15 s)
  och `solinteg-soc25-recovery-third-deadline.timer` (max 60 minuter)

48- och 49-provens rotfiler, historik och status får inte skrivas över.

## Styrning och skydd

Den ursprungliga installerade ARMED-dispatchern är fortfarande den enda
process som normalt skickar Modbus-skrivningar. Dess verifierade
processlokala omslag gör återhämtning under **26,5 %**, till **28 %**,
med högst **1 500 W avsiktligt nätassisterad** batteriladdning; ett
större uppmätt solöverskott får fortsatt utnyttjas inom befintlig
fas-/L3-begränsning.

Förkontroll kräver: aktuellt SoC **25–26 %**, färsk 5-minuters
CT-historik, L1/L2/L3-importprognos inom gränserna, färsk EVCC
**A1** (fysiskt urkopplad med **null**, ej mätt 0 A), aktiv
ordinarie watchdog/poller, oförändrad installerad dispatcher-SHA,
båda gamla prov terminala och deras timers avstängda. Ingen
EVCC- eller inverter-/BMS-golvinställning ändras.

En fristående rot-watchdog avbryter direkt vid EVCC-anslutning,
temperatur icke inom 5–45 °C, SoC under 23 %, osäker CT/fasbalans,
3 200 W per fas eller 6 500 W nätimport, överladdning eller
modifikation av pinned-koden. Under lägesbyte kan **enbart** en
batteri-vs-AC-registermismatch i General/Auto (första 120 s,
SoC 24–28 %, 0–1 500 W batteriurladdning) avvaktas under
**180 s maximal nonresponse**. Verkliga CT-/fas-/EVCC-/temperaturfel
är aldrig undantagna.

Framgång kräver **minst två skilda, minst 20 s separerade
fysiska laddningsmätningar** i EMS `0x303`, med minst 100 W in i
batteriet (`battery_w <= -100`), och efterföljande bekräftad
SoC **minst 28 %**. Vid framgång, uteblivet svar, safety-abort
eller efter max 60 minuter avlägsnas endast den SHA-verifierade
50-konfigurationen, originaldispatchern återstartas och båda
testtimers inaktiveras. Alla testsessioner är engångsprov.

Detta ger inte hårdvaruskydd i realtid; CT-signaler är samplade och
växelriktarens egna skydd kvarstår som sista barriär. Se separat
[ADR](../../architecture/adr-2026-11-01-soc25-recovery-third.md).

## Körning

Läs endast förkontroll:

```bash
sudo -n python3 scripts/tools/start_soc25_recovery_third.py --check
```

Fysisk start med separat godkännande:

```bash
sudo -n python3 scripts/tools/start_soc25_recovery_third.py --start --confirm
```

Status, explicit stopp och timerinsyn:

```bash
sudo -n python3 /usr/local/libexec/solinteg-soc25-recovery-third/soc25_recovery_third_watchdog.py --status
sudo -n python3 /usr/local/libexec/solinteg-soc25-recovery-third/soc25_recovery_third_watchdog.py --stop
systemctl list-timers --all | grep solinteg-soc25-recovery-third
```

Kontrollera fysisk effekt via `/opt/solinteg/live.json` och
`control_actions` i read-only `/opt/solinteg/telemetry.db`.
Försöksstatus `running` bevisar inte laddning; verifiera faktisk
negativ `battery_w` och slutlig SoC. Efter återställning är
det ordinarie planoptimeraren som bestämmer fortsatt drift.

## Fysiskt utfall – 2026-11-01 kl. 21:09–21:10 CET

Fakta verifierade mot root-journal, ursprunglig dispatchlogg och
SQLite `readings` (samtliga tider svensk vintertid):

- **21:09:30:** Tredje isolerade startgodkännandet passerade,
  SoC **25 %**, EVCC **A1** fysiskt urkopplad med `null`-strömmar.
  Original 48- och 49-proven var terminala, 49/49 tidigare
  optimizer-planrader återställda. Startskriptet aktiverade endast
  fristående root-ägt `50-`-tillägg och 60-minuters deadline.
- **21:09:40:** Oberoende första watchdogkontrollen `running`,
  batteri **+258 W** urladdning, SoC **25 %**, oförändrad original
  dispatcher-SHA och båda nya systemd-timers aktiva. Den nya
  processlokala dispatchern kördes med 30s intervall.
- **21:09:43–21:09:51:** Originaldispatchern loggade
  `force_charge: 1500 W` och `accepted_pending_physical_confirmation`
  med återhämtningsmål 28 %. Detta är ett styrkommando, inte
  i sig bevis på fysiskt stabil laddning.
- **21:09:52:** Första efterföljande polleravläsning:
  SoC **25 %**, faktisk batterisignal **−1 692 W** (laddning),
  `work_mode_raw=0x303`, AC-utgång **+978 W**, PV **0 W**,
  huslast **989 W**, netto nät **−11 W** (import),
  L1/L2/L3 **−115/−126/+231 W**. CT/house/AC stämde:
  `978 - (-11) - 989 = 0`, och faserna summerade till
  `−10 W`. Men **batteri/AC-skiftet var 2 670 W**
  (`abs(978 - (-1692))`), långt över den fristående
  säkerhetskontrollens tolerans. Sådana värden kan inte
  behandlas som en konsekvent samtidig batteri-/nätenergibalans.
- **21:09:56–21:10:04:** Watchdoggen avbröt omedelbart med
  `battery/inverter AC energy balance invalid`. Den tog bort
  **endast** den eget SHA-verifierade 50-konfigurationen,
  återstartade den byte-identiska ordinarie dispatchern och
  avaktiverade båda 50-timers. Root-journalen visar
  `state=aborted`, `charge_samples=0`,
  `physical_control_authorized=false` och återställningstid
  **21:10:04 CET**. Ingen SoC-28-avläsning erhölls.
- **21:10:22:** Nästa pollervärden under originaldispatchern:
  SoC **25 %**, batteri **+233 W** urladdning, AC **199 W**,
  nät **−793 W**, hus **992 W** och `work_mode_raw=0x303`.
  Ordinarie dispatcher, poller och standardwatchdog var aktiva.
  48-/49-/50-överlagringar saknades; alla särskilda timers
  avstängda. Äldre rotstater och EVCC orörda.

**Bedömning:** Det finns ett kort batteriladdningsindicerande
mätvärde, men det är **inte** tillräckligt för att bekräfta
stabil och korrekt 25→28 %-laddning. En sekventiell
Modbus-avläsning som passerat ett inverterlägebyte är en
möjlig förklaring till den felaktiga samtidiga balansen,
men detta är inte bekräftat. Det gamla watchdogstoppet
skall betraktas som ett korrekt säkerhetsbeslut.

**Fortsatt åtgärd:** Återanvänd aldrig 48-, 49- eller 50-status,
och starta inte ett fjärde prov innan Modbus-avläsningens
koherens/tidsstämpling och den föreslagna single-writer-brokern
har granskats separat. Ändra inte hårdvarans SoC-golv som
genväg. Detta dokument är en terminal provlogg.

## Tester

```bash
python3 -m unittest scripts.tests.test_soc25_recovery_third scripts.tests.test_soc25_reserve -v
python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q
systemd-analyze verify deploy/solinteg-soc25-recovery-third-*.service deploy/solinteg-soc25-recovery-third-*.timer
```
