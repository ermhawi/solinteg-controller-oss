# Nattligt Wi-Fi-/Modbusavbrott: ASUS omstartsschema 2026-11-03

**Status:** Rotorsak för det första avbrottet **verifierad** och
omstartsschemat på ASUS avaktiverat. Det andra avbrottets exakta
verkningskedja är **inte fullständigt bevisad**. Detta är en separat
nätverksåtgärd, **ingen inverter-/Modbusändring**.

## Tidslinje (CET, UTC+1)

| Lokal tid | Källa | Belägg |
| --- | --- | --- |
| 03:00 | ASUS NVRAM + uptime | `reboot_schedule_enable=1`, `reboot_schedule=11111110300`: schemalagd **daglig** omstart kl. 03:00. Uptime och logg efter start stämmer med en faktisk omstart. |
| 03:00:27 | Dell NetworkManager | `wlp6s0` går från `completed` till `disconnected`. DHCP startas om. |
| 03:00:30–03:00:42 | Dell kernel/NM | Misslyckad autentisering mot AP; `ssid-not-found` och `link timed out`. |
| 03:02:09 | Dell dispatcher | Modbusanslutningen misslyckas med **`Network is unreachable`**; `return_to_auto` misslyckas också. |
| 03:02:14 | Dell kernel/NM | Wi-Fi återassocieras och WPA-handshake slutförs. |
| 03:03:39 | Dell poller | Extended-CT-läsningar återhämtade efter timeout. |
| 03:12:09 | ASUS syslog | `wanduck` begär `restart_wan_if 0`. |
| 03:12:21 | Dell NM | Nätverkstillstånd `CONNECTED_SITE` (internet ej verifierat). Ingen ny Wi-Fi-deassociation. |
| 03:12:41–03:13:01 | Dell dispatcher | Modbus `No response received after 3 retries` och efterföljande General-revert misslyckas. |
| 03:13:09–03:13:14 | ASUS syslog | `ISP's DHCP did not function properly`, därefter `WAN was restored`. |
| 03:13:24 | Dell NM | Nätverkstillstånd `CONNECTED_GLOBAL` igen. |
| 03:13:55–03:15:25 | Dell poller | Ogiltig/trunkerad Modbusframe, därefter återhämtade CT-läsningar. |

**Slutsatser:** Första avbrottet är säkert ett Wi-Fi-bortfall som
sammanfaller med routerns **avsiktliga dagliga omstart**.
Dells NetworkManager-historik bekräftar samma avbrott kl. **03:00
varje dag 28 oktober–3 november 2026**, inklusive `link timed out`
och misslyckad Wi-Fi-aktivering. Detta är **sju dagar i följd**,
inte ett isolerat radioproblem.
Andra avbrottet sammanfaller med en **WAN/DHCP-restarter** på
routern, men Dell var fortfarande associerad till Wi-Fi. En
kausal WAN→lokal Modbus-dongel-kedja är en **hypotes**:
dongelns kända cloud-upload-konkurrens eller tillfälliga
Modbus-TCP-fel kan ha bidragit. Ingen ny TCP:502-probe gjordes.

## Genomförd och verifierad åtgärd

På den befintliga ASUS-routern via skyddad SSH:

- Före: `reboot_schedule_enable=1`, schema **varje dag 03:00**.
- Enda förändring: `nvram set reboot_schedule_enable=0`
  följt av `nvram commit`.
- Efter: `reboot_schedule_enable=0`, ursprungligt schema
  oförändrat (kan återaktiveras), kontinuerlig router-uptime.
- **Ingen router-/Wi-Fi-/WAN-omstart utfördes**. Dells Wi-Fi
  var efteråt fortsatt anslutet; aktiv poller, dispatcher,
  webb, ordinarie watchdog och fristående broker-observatör
  var aktiva. Brokern fortfarande inaktiv, observerstatus
  `healthy` och `physical_writes=false`.

**Rollback om den schemalagda omstarten av någon anledning behöver
återinföras** (på routern, manuellt och endast vid godkänt
underhållsfönster):

```sh
nvram set reboot_schedule_enable=1
nvram commit
```

Detta återställer den tidigare dagliga 03:00-tiden eftersom
`reboot_schedule` medvetet lämnades oförändrat. Automatisk
daglig omstart är dock inte kompatibel med pålitlig
ladd-/urladdningsstyrning över samma Wi-Fi.

## Avgränsning och nästa kontroll

- **Första natten verifierad 2026-11-04:** routern var fortfarande
  igång med drygt ett dygns uptime och `reboot_schedule_enable=0`.
  Dell behöll `wlp6s0` vid 03:00. Fönstret 02:45–03:35 CET
  innehöll 99 giltiga mätningar med högst 30,8 s mellan,
  utan Modbusfel eller misslyckad återställning. Se
  [nattverifiering och kvarstående protokollfel](solinteg-modbus-overnight-2026-11-04.md).
  Detta bekräftar **en natt**, inte framtida felri drift.
- Följ separat upp `wanduck`/WAN-DHCP, särskilt om
  Modbusframefel återkommer **utan** routeromstart. En WAN-restart
  ska inte utan ytterligare bevis antas vara grundorsaken för
  lokal TCP:502, trots tidsmässig överlappning.
- Dell använder Wi-Fi via Intel Wireless-AC 7260/`iwlwifi`,
  5 GHz och ungefär **−70 dBm** vid efterkontroll.
  Power save är redan **off**; att slå av den igen hjälper
  inte. Ett dedikerat nätverk med Ethernet/USB-Ethernet och
  robust AP är fortsatt att föredra inför fysisk brokerdrift.
- Även när schemat är avstängt kvarstår **Dell som single
  failure domain**, gamla watchdoggens direkta Auto-fallback
  och inga garantier för Modbus ägarskap/fencing.
- **Firmware på växelriktaren** är fortfarande okänd och bör
  hämtas genom tillverkarens read-only status, inte genom
  otestade `100xx`-register. Se
  [fallbackinventeringen](solinteg-fallback-readonly-inventory-2026-11-03.md).

### Tillverkarkällor

ASUS dokumenterar att schemalagd routeromstart tillfälligt
bryter nätverk och att felsökning av återkommande omstarter
bör börja med att avaktivera `Reboot Scheduler`:
- [ASUS, set up scheduled restart](https://www.asus.com/us/support/faq/1049258/)
- [ASUS, router restarts automatically](https://rog.asus.com/se/support/faq/1050723/)

**Sekretess:** dokumentet innehåller inga privata IP-adresser,
MAC, SSID, kontouppgifter, råa nätverksloggar eller andra
installationshemligheter. Tidslinjen är avidentifierad.
