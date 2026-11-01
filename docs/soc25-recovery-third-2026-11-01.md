# Tredje övervakade återhämtningsprovet, 25–26 → 28 % (2026-11-01)

**Status:** Ny, fristående och tidsbegränsad providentitet implementerad.
Fysisk status verifieras separat från provets root-journal. Detta är
**inte ett permanent 25 %-SoC-skydd**.

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
[ADR](architecture/adr-2026-11-01-soc25-recovery-third.md).

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

## Tester

```bash
python3 -m unittest scripts.tests.test_soc25_recovery_third scripts.tests.test_soc25_reserve -v
python3 -m unittest discover -s scripts/tests -p 'test_*.py' -q
systemd-analyze verify deploy/solinteg-soc25-recovery-third-*.service deploy/solinteg-soc25-recovery-third-*.timer
```
