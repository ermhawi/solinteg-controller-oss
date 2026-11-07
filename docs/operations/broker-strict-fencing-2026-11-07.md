# S07 · Konservativ opt-in-spärr efter osäker brokertransaktion

**2026-11-07 · `tested/source`, INTE installerad.** Den tidigare [felinjektionen](broker-fault-injection-2026-11-07.md) reproducerade att en begäran med **nytt request-ID** kunde gå vidare efter en tvetydig `transaction_charge`-timeout. Den här iterationen inför en strikt **default OFF** spärr med ett separat uttryckligt driftsval: `SOLINTEG_BROKER_STRICT_TRANSACTION_TIMEOUT_LATCH=1`. Inga produktionsflaggor har ändrats.

## Faktiskt nytt kontrakt i källkod

- `modbus_broker_control_service.build_control_broker` accepterar enbart `0`/`1` och passerar en boolesk inställning; frånvarande variabel är `0`. Ett strikt läge kräver redan beväpnad `control`-broker med fulla transaktioner.
- `modbus_broker.Broker.submit` stämplar den befintliga generationsräknaren och sätter `_safety_latched=True` vid **tvetydigt IPC-timeout** under en `transaction_*`. Normal `transaction_charge/discharge/auto/hold/freeze/importlimit` med ett **nytt request-ID** nekas som `Fenced` innan den läggs på kön. Ett **återkommande samma request-ID** hämtar endast befintlig Futures slutliga fel/kvitto: ingen ny FC06.
- Om en normal `transaction_*` får ett `OSError` eller ett avbrutet registersteg i worker, sätts samma in-memory-latch och en ny generation; senare kommandon inhiberas. `status` tillhandahåller `strict_transaction_fencing`, `safety_latched` och `last_fence_reason` som **källdiagnostik, inte oberoende fysisk säkerhet**.
- FC03 och `status` fortsätter vara möjliga för läsande diagnostik efter låsning. **Inga dolda retries** och ingen annan TCP-klient skapas av spärren. Endast separat godkänd S06-supervisor kan eventuellt begära `safety_auto`, där S06:s färska SoC/EMS-/ägarskapsgrind fortfarande gäller; de två opt-in-flaggorna är oberoende och båda är OFF som standard.
- Vid tvetydig transaktion med **en eller flera redan utförda FC06** är tillståndet fortsatt **unknown**. En blockerad normal dispatcher kan inte längre utföra sin vanliga `transaction_auto`-rollback i strikt läge; detta är en **avsiktlig source-only konservativ begränsning**, inte ett påstående att hårdvaran blivit säker.

## Reproducerbara syntetiska prov

`scripts/tests/test_broker_strict_timeout_latch.py` **5/5** med existerande `FakeDongle` och lokal UDS:

| Test | Källbeteende med flaggan explicit ON |
| --- | --- |
| Första FC06 fastnar, andra transaktion köas, första client timeout | `safety_latched=True`, väntande request `Fenced`, ny request-ID `Fenced`; `50000=EMS` skickas inte, `peak_clients=1` |
| FC06-fel vid steg 1, 3 och 5 | Ingen blind retry, exakt antal skickade registersteg; latched unknown efter exception, nytt request-ID nekat |
| Samma request-ID efter fel | Tidigare fel återrapporteras via cache, **0 nya FC06** |
| Diagnostik via FC03 efter latch | Färsk registerläsning tillåten, ingen normal skrivning |
| Flagga utelämnad/ogiltig eller inte armed+transaction-runtime | OFF eller byggstopp före TCP; `true`, `yes`, `2` och icke-godkänd runtime nekas |

Tidigare `scripts/tests/test_broker_fault_matrix.py` **3/3** och S06 **8/8** passerar i samma isolerade miljö. Testerna kör enbart lokal UDS och fake-register; **ingen fysisk 502/TCP, EVCC eller aktiv systemd** har anropats.

## Kända NO-GO-gränser

1. **Latchen är avsiktligt processlokal.** Efter att brokerprocessen dör eller startar om finns ingen säker beständig incidentmarker eller bevis på återtagen fysisk socket. Det måste lösas i **S08**, med rätt process-/lease-/socketfencing. En omstart rensar inte inverterfirmwarens eventuella kvarstående EMS-börvärde.
2. `_safety_latched` efter en delvis lyckad skrivsekvens ger **inte** en garanterad General-restaurering. Om EMS står kvar saknas separat OEM-/BMS-verifierad effektspärr.
3. Strikt mode är **medvetet OFF för att inte ändra installerad legacy rollback**, särskilt när dispatchern idag fortfarande använder äldre `best-effort Auto`. Inga självaktiverande inställningar eller säkerhetsprioriteringar har driftsatts.
4. Extern direkt-TCP och hostens fullständiga strömavbrott kan inte lösas av en in-process-`flock` eller en socketserver på Dell. Fysisk release kräver V02/V03/V04 och ett uttryckligt GO.

**Definition of Done för S07:** den syntetiska fel-/timeout-/reconnectmatrisen har deterministisk inhibition av nya request-ID och **inga extra writers** med strikt opt-in. Statusen är endast `source/tested`. Beständig latch och processåtertagning är en **separat öppen S08**; att S07 är testad tillåter inte fysisk aktivering.
