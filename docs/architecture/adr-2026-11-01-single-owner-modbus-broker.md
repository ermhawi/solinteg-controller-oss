# ADR 2026-11-01: en ägare för Solintegs Modbus TCP

**Status: proposed** · **Fysisk driftsättning: inte godkänd** ·
Beslut gäller först efter granskning av [migrationsplanen](../plans/modbus-broker-2026-11-01.md).

## Kontext

Pollern använder en persistent TCP-klient och dispatcher/watchdog öppnar
separata korta Modbusförbindelser. Det har fungerat i vardagen, men
dongelns begränsade parallellism och cloud-stopp på 4–8 s innebär risk
för kolliderande transaktioner. Pollern läser CT/AC, batteri, SoC och
driftläge i olika FC03-anrop. Tre SoC25-prov avbröts säkert, varav det
tredje på ett batteri-/AC-värde som inte var energimässigt förenligt
med CT/hus. Registertidsförskjutning är ännu inte bekräftad som rotorsak.

Nuvarande watchdog är en **separat säkerhetsprocess** som direkt ansluter
till invertern för att återställa General/Auto om dispatchern dör.
En ny arkitektur får inte förlora denna oberoende barriär eller anta
att IPC-tillgänglighet ensam garanterar fysisk Auto.

## Föreslaget beslut

- En lokal, systemd-ägd **broker** har ensam ordinarie TCP/FC03/FC06-
  förbindelse mot invertern. Poller, dispatcher och watchdog använder
  ett skyddat Unix-domain socket-API med rollspecifika behörigheter.
- Kontrollen exponeras som hela **semantiska transaktioner**
  (`charge`, `discharge`, `hold`, `auto`, `safety_auto`), aldrig
  obegränsade klientstyrda registerwrites. Mode/caps/target-ordning,
  rate limit, arming, SoC-/fasgränser och readback bevaras. Watchdog
  får prioritet och fence:ar äldre kön/generationer.
- Pollern får serialiserade **tidsmärkta registerblock**, en samplad
  start-/sluttid och mode före/efter FC03-serien. Inkonsekventa eller
  för långa samples *avvisas* och arkiveras diagnostiskt, inte
  märks som färsk giltig telemetri. Detta är inte en garanti för
  atomära inverterregister.
- En **fristående** säkerhetsprocess övervakar dispatcher och broker.
  Om brokerprocessen dör/hänger får en **separat privilegierad
  nödfallback** skriva Auto endast efter verifierad fencing/stopp av
  brokerägaren och dess socket. Lyckad readback och larm ingår.
  När fallbackens exklusivitet inte kan garanteras ska den inte
  tävla med en aktiv broker; larma och överlåt kvarvarande fysiskt
  skydd till inverterns native BMS/SoC-säkerhet.
- Migrera **offline fake → kvalitetsmodell → live read-only poller →
  säker kontroll/rollback → exklusiv direktaccess**. Varje live-etapp
  har separat godkännande och verifierad rollback. Ingen ändring
  av EVCC/Waveshares RS-485-styrning.

## Alternativ och avvägningar

| Alternativ | Bedömning |
| --- | --- |
| Flera direkta Modbusklienter + längre timeout | Minskad symptomrisk men ingen ensam skrivägare eller koherent kontrollsekvens |
| Delad `flock` mellan dagens klienter | Hindrar en del samtidiga lokala anrop men inte omotiverad tredje klient, verktyg, halvfärdiga flerregisterkommandon eller telemetriska mode-skiften; risk för låst watchdog |
| En broker med semantiskt API och separat fencead nödfallback | Mer kod/driftkomplexitet men explicit ägande, korrekt prioritets- och generationsmodell och tydliga verifierbara felvägar |

## Konsekvenser / beslutspunkter

- IPC/UDS kräver rolluppdelning; dagens tre systemd-tjänster använder
  alla samma `User=solinteg`.
- Den lokala brokeranslutningen kan fortfarande fastna under
  dongelns cloudarbete. Skriv- och safety-latens måste mätas
  mot konservativa deadlines; inga hårda tidslöften utan tester.
- Den operativa cutovern får aldrig samtidigt starta original
  poller/dispatcher med ny broker som om exklusivitet redan gällde.
- Safety-fence och tolkningen av snapshotkvalitet är **öppna designval**
  som ska verifieras offline innan kontrollsteg får föreslås för drift.
- Fram till ett godkänt implementationstest gäller nuvarande aktiva
  dispatcher, poller, watchdog och befintligt BMS-golv oförändrade.
  **Inget fjärde fysiskt SoC25-test ingår i detta beslut.**

**Relaterat:** [migrationsplan](../plans/modbus-broker-2026-11-01.md),
[Modbusregister](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/MODBUS.md),
[tredje SoC25-incident](../soc25-recovery-third-2026-11-01.md).
