# S07 · Brokerfelinjektion utan fysisk Modbus (2026-11-07)

**Status: source/tested; ingen ändring av broker/runtime.** [S05:s single-owner ADR](../architecture/adr-2026-11-07-modbus-fencing.md) och [historisk transaktions-ADR](../architecture/adr-2026-11-04-broker-complete-transactions.md) är styrande. `scripts/tests/test_broker_fault_matrix.py` använder existerande `FakeDongle`, worker och lokalt UDS från `test_modbus_broker_transactions.py`. Den öppnar inga fysiska TCP-anslutningar.

| Injektion | Verifierat utfall | Begränsning |
| --- | --- | --- |
| FC06 undantag vid steg 1, 2, 3, 4, 5 i `transaction_charge` | 5/5 ger `OSError` via IPC, exakt ett försök på det felande registersteget, ingen implicit retry, mode ändras inte till EMS | Det kan finnas tidigare lyckade registerskrivningar: **okänt** fysiskt mellanläge, ej atomär rollback |
| Samma `request_id` efter ett fel | Samma cached failure, **0** ytterligare FC06 | En *ny* request-ID efter tvetydig timeout kan fortfarande passera annan kontroll; kräver högre nivåns spärr |
| FC06-blockerat vid `50207`, två parallella styrförfrågningar | Första timar ut med `TimeoutError`, `generation` räknas upp, köad andra begäran stängs av med `Fenced`/fel, **ingen** `50000=EMS` | Broker saknar en beständig `safety_latched` för transaktionstimeout: ett senare *nytt* kommando kan fortfarande accepteras |
| Transport kopplas ned syntetiskt efter lyckad transaktion | Nästa FC03 skapar ny anslutning **först efter** att gammal stängts: totalt två sekventiella clients, `peak=1`; ingen upprepad FC06 av tidigare operation/request-ID | Riktig nätåteranslutning och processdöd är inte kvalificerade här |

**Resultat:** 3/3 nya fokuserade tester, plus befintliga fakes för behörigheter, concurrent poller, idempotens, readback, fas/SoC och rollback. S07:s observerade semantik `TimeoutError` = **unknown result**, `Fenced` = **inhibited queued command**; API:t returnerar **inte** ett atomärt kvitto av fysisk effekt. Den kvarvarande risken är **en ny request-ID utan extern latch efter en timeout**: S07 får inte användas som underlag för godkänd självåterställning, och varje följande live-release är **NO-GO** tills beslut av S06/S08. Ingen ny recovery, direkt-TCP-fallback, systemd-åtgärd eller fysiskt Modbus-kommando har genomförts.
