# Sundsvall 2026-11-04: nattverifiering och Modbus-svarsförväxling

**Status:** Skrivskyddad driftinventering kl. 05:15 CET den 4 november.
ASUS daglig omstart **åtgärdad och första natten verifierad**.
Kvarstående Modbusprotokollfel **inte åtgärdade** och exakt mekanism
inte verifierad med paketspårning. **Ingen Modbus-, EMS-, router- eller
systemd-konfiguration ändrades i denna kontroll.**

## 1. ASUS / Dell efter åtgärden

Föregående natt 2026-11-03 startade ASUS om vid sitt schemalagda
03:00-fönster; Dell tappade `wlp6s0` och `return_to_auto`
misslyckades med `Network is unreachable`.
Schemat `reboot_schedule_enable` ändrades därefter från `1` till
`0` och `nvram commit` verifierades.

**Första nästa natt, 2026-11-04:**

| Kontroll | Verifierat |
| --- | --- |
| ASUS NVRAM | `reboot_schedule_enable=0`; ursprungligt schema bevarat |
| ASUS uptime kl. 05:15 CET | Mer än 1 dygn och 2 timmar, alltså ingen nattlig routeromstart |
| Dell NetworkManager och kernel vid 03:00 | Inga `wlp6s0`-autentiserings-, deassocierings- eller `ssid-not-found`-händelser |
| Pollerlogg kl. 02:45–03:35 CET | Inga fel |
| SQLite `mode=ro` under samma fönster | 99 mätningar, max **30,8 s** mellan lagrade prover; inga luckor över 45 s |
| Kontrollåtgärder under samma fönster | Inga `error_revert_failed` |
| Samtliga styrtjänster | Direktpoller, dispatcher, ordinarie watchdog, webb och Dell-observatör aktiva; broker ej aktiverad |

**Resultat:** Den specifika schemalagda 03:00-störningen uteblev.
Det visar utfallet under **en natt**, inte att all framtida
nätverks-/Modbuskommunikation är säker. Router-WAN-bortfallet
03:12 den tidigare natten var en separat incident.

## 2. Fortsatta Modbusfel trots stabilt Wi-Fi

Hela perioden 00:00–05:15 CET kontrollerades separat i lokal SQLite,
journalctl och NetworkManager. **625 giltiga SQLite-läsningar**;
de två största luckorna var vardera **60,2 s**. Alla **85**
registrerade kontrollåtgärder hade utfallet `applied`,
men lyckade writes är inte ett bevis för frånvaro av transient
protokollförväxling.

| Lokal tid 4 nov | Poller/dispatcher | Samband |
| --- | --- | --- |
| **02:35:26** | Pollerns FC03-begäran om CT-register `10994` fick **`WriteSingleRegisterResponse` för `50207`**, `registers=[65506]`; efterföljande frame kunde inte avkodas | `65506` motsvarar signerad `-30` rått, dvs. 300 W laddning. Dispatcher registrerade `charge 300 W` som `applied` kl. 02:35:31 |
| **03:55:48** | Pollern fick åter `WriteSingleRegisterResponse` för `50207`, nu `registers=[5]`, och förkastade nästa avkodning | `5` motsvarar 50 W urladdning. Dispatcher registrerade `discharge 50 W` som `applied` kl. 03:55:53 |
| **04:40:59** | Dispatcher loggade `Invalid Modbus protocol id: 18687` | Kontroll utförd efter felet; inga felutfall i SQLite |
| **05:00:35** | Poller loggade `Invalid Modbus protocol id: 515` | Nästa giltiga telemetri efter cirka 40 s; inga rapporterade Wi-Fi-deassociationer |

Pollerfel vid 02:35 och 03:55 ledde till luckor om **60,2 s**.
Pollern accepterade **inte** 50207-skrivsvaren som giltig
CT-telemetri; efter nästa poll återkom normala värden.
Mätluckorna ligger **utanför 03:00-verifieringens fönster**.

Den här evidensen ger en **stark misstanke om felaktig
transaktions-/svarsmultiplexning när poller och dispatcher
använder flera Modbus-TCP-sessioner mot samma dongel**.
De två oavsiktligt lästa FC06-svaren innehåller just de
effektbörvärden som dispatchern bekräftade några sekunder
senare. Bevisnivån är **tids- och registerkorrelation**,
inte fångade TCP-paket, verifierad gatewayimplementering
eller bevis på exakt hur transporten korsade sessioner.

Det återstår att avgöra om `Invalid Modbus protocol id`
har samma orsak. En separat, aktiv FC03-probe för att
undersöka detta vore olämplig på den redan delade dongeln.

## 3. Beslut och återstående arbete

1. Behåll avaktiverat ASUS-schema. Ändra inte andra router-,
   Wi-Fi- eller WAN-inställningar utan separat utredning.
2. **Prioritera en enda Modbus-TCP-ägare:** brokerplanens
   FC03-only read-only-cutover ska få separat Go/No-Go,
   avsluta gamla pollerns socket före brokerstart och
   vara helt återställbar. Att starta en parallell broker
   skulle **förvärra** problemet.
3. Före aktiv styrväg/fallback krävs dessutom fungerande
   serialiserad FC03/FC06, fysisk läges-/effektreadback,
   process-/nätverksfencing, oberoende säkerhetssupervisor
   och tillverkarbekräftelse för EMS/SoC-skydd.
4. Modellen MHT-10K-25 har fortfarande **okänd
   ARM-/DSP-/BMS-firmware**. Ska hämtas via OLED eller
   auktoriserad read-only molnstatus, inte via ny
   `100xx`-Modbusprobe.
5. Säkerhetsstatus för automatisk fysisk General/EMS-fallback:
   **NO-GO**. Denna observation har **inte**
   implementerat säker neutralisering av gammal `0x0303`.

Relaterade dokument:
- [Första nattincidenten och routerändringens rollback](solinteg-network-incident-2026-11-03.md)
- [Brokerplan och separata cutovergrindar](plans/modbus-broker-2026-11-01.md)
- [Read-only brokerstadium och tester](modbus-broker-readonly-stage3-2026-11-02.md)
- [EMS kontra General och SoC-fallback](architecture/adr-2026-11-02-ems-general-failsafe.md)

**Sekretess:** publicera inte faktiskt IP/MAC/SSID,
hushållets förbrukningskurvor, hemligheter eller exakta
driftloggar i den publika grenen.
