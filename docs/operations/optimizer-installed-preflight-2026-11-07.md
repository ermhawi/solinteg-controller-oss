# O01 · Läsande jämförelse mellan optimerarens källkod och aktiv drift

**2026-11-07, `source/installed-observed`, ej paritetsgodkänd.** Läsning av `/opt/solinteg/app` med `sudo -n sha256sum`, `systemctl show` och vitlistad env-status från den **aktiva** `solinteg-web.service`. Ingen installerad fil, process, SQLite-rad, timer, plan, Modbus eller EVCC har ändrats. Endast filhashar och de nedan uppräknade *icke-hemliga flaggstatusarna* redovisas; ingen rå miljöfil eller datarad har loggats.

| Produktionsmodul | Git `Sundsvall` SHA-256 (16) | Aktiv installerad SHA-256 (16) | Byteparitet |
| --- | --- | --- | --- |
| `lib/optimizer.ts` | `1067a61ef1d9a04e` | `afce0bdff0ed041c` | **NEJ** |
| `lib/plan.ts` | `d8082c625b07caae` | `17c1053039bdedd8` | **NEJ** |
| `lib/prices.ts` | `769b697e71b50426` | `4e169613471ccbb2` | **NEJ** |
| `lib/pipeline.ts` | `4348c59769af418d` | samma | JA |
| `lib/load.ts` | `04c203a61185da60` | samma | JA |
| `lib/forecast.ts` | `bbb6a91bcb2d8e03` | samma | JA |
| `lib/telemetry/dispatch.ts` | `2289aa0e3697ece6` | `cc1ed5ec3e9c4d10` | **NEJ** |
| `lib/telemetry/core.ts` | `6ed6a74282d8279e` | samma | JA |
| `lib/horizon.ts` | `1cd70b57c2cd58aa` | samma | JA |
| `lib/constants.ts` | `35842b661014239d` | `2e4932e51d705f37` | **NEJ** |

**Aktiv enhet:** `solinteg-web.service` = `active`; `SOLINTEG_CONTROL_ARMED` = **ENABLED**. `SOLINTEG_STRICT_PLAN_PUBLICATION` och `SOLINTEG_MIN_PLAN_HORIZON_HOURS` = **UNSET** (så O06/O04:s nya källkod är inte installerad/aktiverad). Även `SOLINTEG_HOLD_ENABLED` och `SOLINTEG_REFUND_CREDIT` = **UNSET** i observerad webbenhet. `TELEMETRY_DB_PATH` = **SET** utan att avslöja sökvägen.

**SQLite** (URI `mode=ro`): `optimizer_runs` innehöll de förväntade sju kolumnerna `id,logged_at,price_date,has_tomorrow,start_soc_kwh,inputs_json,dispatch_json`; **12 366** rader, senaste loggat cirka **0,1 h** före denna kontroll. Schema-/radräkning är **inte** en kontroll av innehållet, planens nyttovärde, aktiva dispatcher-kommandon, fysisk SoC eller korrekt tidsaxel.

## Konsekvenser

1. Aktiv prisplan kan inte anses binärt eller semantiskt identisk med Git-HEAD: **fem av tio** granskade källmoduler skiljer, särskilt `optimizer.ts`, `prices.ts` och `constants.ts`. [R04-klassificeringen](../web-and-broker-source-runtime-diff-2026-11-07.md) redovisar kända marginal- och DST-avvikelser. Källkodens gröna replay är inte ett GO att installera den som helhet.
2. O04:s kort-horisontsspärr och O06:s nyare schema-/SoC-gate är explicit **default-off och inte driftsatta**. Därmed är den observerade färska SQLite-raden *inte* bevis för att de nya spärrarna fungerar i produktion.
3. Nästa isolerade O01-steg är en reproducerbar **side-by-side**-replay mot frysta kopior av de två optimizer-/pris-/konfigvarianterna med **samma** syntetiska och historiska indata, jämförd per beslutslot, terminal-SoC och faktisk kostnadsmodell. Sådan replay är ännu inte utförd här. Läs den installerade bundlen/effektiv miljö (utan att dela hemligheter) innan ett fullständigt V03-/R07-rollbackpaket kan godkännas.

**O01 är fortsatt ÖPPEN.** Denna read-only komponentinventering minskar oklarheten om *vilken* kod som körs, men inte den avgörande frågan om samma indata ger samma ekonomiska och fysiskt kvalificerade beslut. Ingen runtime- eller fysisk ändring gjordes.
