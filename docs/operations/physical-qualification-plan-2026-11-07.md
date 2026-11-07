# V01 · Fysisk kvalificeringsmatris (endast plan, NO-GO)

**Version 2026-11-07 · status source/tested, ingen fysisk exekvering eller release godkänd.** Styrande maskinläsbart och versionshanterat underlag: [physical-qualification-matrix-2026-11-07.json](physical-qualification-matrix-2026-11-07.json). Det innehåller fyra **separata** releasegrupper, respektive risk-/rollbackansvarig *roll*, förvillkor, observationsvariabler, omedelbara stopporsaker och selektiv rollback. Ingen grupp får godkännas samtidigt med en annan.

| Område | Ansvarig operatörsroll | Fysisk verifiering / omedelbar STOPP | Återställningsägare |
| --- | --- | --- | --- |
| Broker / failsafe | Modbus-ägare | En enda fysisk TCP-ägare och brokerlease; stoppa på FC06-timeout, okänd SoC/CT, dubbla skrivare eller registermismatch | Oberoende recovery-operatör |
| Dispatcher / energipolicy | Energistyrningsoperatör | SoC-/PV-/L3-/EV-telemetri och fysisk W; stoppa vid oväntad batteri→bil, fasöverträdelse, okänd EMS-state eller osäker manual intervention | Runtimeoperatör |
| Optimerare och webb | Planerings-/webboperatör | Planrevision, DST, live-SoC, HTTP/OAuth och SQLite-paritet; stoppa vid korrupt eller ofullständig plan | Webb/runtime-operatör |
| EVCC | EV-laddoperatör | Kabel-epoch, 6 A, Volvo Smart Charging, manuella stopp, FC03/FC10-readback; stoppa vid auto-resume efter manuell paus eller okänt skrivresultat | EVCC pause-owner-operatör |

## Före ett eventuellt separat, bemannat GO

1. **Operatören måste komplettera och underteckna** identitet för aktivt ansvarig och rollbackansvarig, exakt omfattning, start-/stopptid, utgåva/SHA och separat godkännandereferens. Fysisk körning är **förbjuden** tills användaren uttryckligen godkänt just det provet.
2. Samla aktuellt och **färskt** läsbart läge: verklig SoC och batteri-W, PV, import/export, L1–L3, native EndSOC/BMS och EMS/General-register, beräknad EV-kabelepok, användarens manuella laddpauser, aktiv brokerprocess/lease samt plan/timer/arbetsflöde. Okänd observation ⇒ NO-GO.
3. Verifiera **fullständig root-ägd rollback**, inte enbart 24 källfiler i R08: exakta installerade filhashar, systemd-unit/drop-ins, privat konfiguration utan exponering, SQLite-konsistent DB-backup och separat Next-build; kontrollera backupens återläsning i isolerad miljö och bevara återställningsreferenser. R07-manifestet med oklassad divergens betyder NO-GO.
4. Frys testscenarier, slutvillkor och observationsfönster i **egen releasegrupp**. Testproceduren får inte innehålla blind `Auto`, otillåtet FC06/FC10, automatisk återstart efter manuell EV-paus, parallella Modbus-ägare eller avsiktlig djupurladdning för att prova ett antaget BMS-golv.
5. Efter separat GO och en godkänd release behövs readback + oberoende fysisk effektobservation samt 24–72 timmars avgränsad uppföljning innan `installed` får upphöjas till `physically_verified`. Avvikelse innebär omedelbar STOPP och rollback via verifierad ensam ägare.

**Test/verifikation:** `scripts/tests/test_physical_qualification_matrix.py` verifierar 3 maskinläsbara kontrakt: `PREPARED-NO-GO`, inget implicit approval/automatiskt rollback, fyra isolerade områden och obligatoriska aktuella mät-/backup-/ägarskapsfält. Dessa tester **utför ingen fysisk qualification**.

**Beslut för S10/V01:** Matrisen är skriven och testad. **S10:s separat godkända bemannade provplan återstår** eftersom ingen fysisk release eller dess exakt avgränsade GO-beslut är godkänd. Följ [native EndSOC-utredning](native-endsoc-safety-gate-2026-11-07.md), [S05-fencing](../architecture/adr-2026-11-07-modbus-fencing.md) och [rollback](private-release-and-rollback-2026-11-07.md).
