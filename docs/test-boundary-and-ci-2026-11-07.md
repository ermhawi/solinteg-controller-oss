# K04/K06 · Testklasser och icke-driftsättande CI-grind

**Status 2026-11-07:** källbaserad GitHub Actions-workflow för **enbart** `Sundsvall`; inget produktions-, systemd-, installerat /opt- eller Modbus/EVCC-arbete godkänt.

| Nivå | Vanligt kommando | Isolering och acceptans |
| --- | --- | --- |
| Ren Python policy/enhet | `python3 -m unittest discover -s scripts/tests -p 'test_*.py'` | `fakes.py` stubbar `pymodbus` och tester injicerar mocks; inga verkliga inverter- eller EVCC-skrivningar. |
| SQLite-/JSON-integration | Samma `unittest`-upptäckt | Tempkataloger/privata in-memory-fixtures, ingen /opt-installation. Verkliga historiska DB-filer ingår **inte**. |
| Fake Modbus/EVCC och Unix-IPC | Samma `unittest`-upptäckt | Syntetisk dongel/EVCC, lokala sockets/mockade transportklasser; inga fysiska FC06/FC10. |
| Syntetisk loopback-smoke | `scripts/tests/wire_smoke_{readonly,control,transactions}.py` | **Manuell** särskild kvalificering av riktig pymodbus mot `127.0.0.1` på ephemeral port, inte i `test_*.py`. |
| Fysiskt växelriktar-/EVCC-prov | `scripts/tools/probe_*.py`, `evcc_physical_qualification.py`, SoC25-/hold-installers | **Utanför CI och vanlig unittest.** Kräver operatörens särskilda godkännande, skydd/rollback, firmwarebevis och fysisk övervakning. [Bevarad fryslista](operations/probe-quarantine-2026-11-07.md). |
| Next.js/Vitest | `npm test`, `npm run typecheck`, `npx tsc --noEmit --noUnusedLocals --noUnusedParameters`, `npm run lint`, `npm run build` | Källmiljö med fixtures/mocks; inga externa autentiserade konnektorer eller kundhemligheter. |
| Manifest och dokumentation | `python3 scripts/tools/release_inventory.py --validate-source-manifest`; `zensical build --strict` | Enbart lokalt källträd; manifestkontrollen **läser inte systemd, /proc, /opt eller nät**. Fysisk releaseparitet kontrolleras separat med read-only driftinventering på operatörens maskin. |

## Isolerad GitHub-körning

Den nya `.github/workflows/quality.yml` använder GitHub-hostad `ubuntu-24.04`, `contents: read`, Python 3.12 och Node 24. Triggers: push/pull_request till `Sundsvall` samt manuell workflow-dispatch; **ingen** installation, SSH, självhostad runner, publik deploy, push, `systemctl` eller skrivbar token.

Preflight nekar körning om `/opt/solinteg/app`, separat EV-runtime eller `.env.local` finns. Dessutom är `SOLINTEG_HOST` uttryckligen **127.0.0.1**, `SOLINTEG_CONTROL_ARMED`, `EVCC_CONTROL_ARMED`, `EVCC_PRICE_CONTROL_ARMED`, `EVCC_MANUAL_TEST`, `EVCC_START_APPROVED`, `DISPATCH_FREEZE_HOLDS` och `DISPATCH_RESERVE_HOLD_VERIFIED` **0**. Inga produktionshemligheter används. Loopback-transport i isolerad runner kan inte kontakta husets private IP; en eventuell kodväg som kräver armering ska avbrytas.

**Käll-/schema-grind:** `release_inventory.py --validate-source-manifest` (schema v1) validerar alla **24 manifestmoduler**, unika säkra relativa sökvägar, existens, 11 unitnamn och transitive lokala EV-importer. Exit 0 = pass; exit 6 = blockerad. Varken andra snapshots eller fysisk paritet blandas in.

**Regressionsstatus före workflow-publicering:** Python `1641/1641` godkända (före tre O07-tester och tre manifest-valideringstester); Vitest `743` godkända/`1` hoppad; `npm run typecheck`, strängare `tsc`, ESLint, Next-build, `zensical build --strict` godkända lokalt. Källmanifest/EV-closure: **15/15** fokuserade tester och `--validate-source-manifest` gav pass. Ingen CI-körning antas grön före verifierad GitHub Actions-status.

**Releasespärr:** denna CI är en **kvalitetsgrind för källan**, inte ett tillstånd att kopiera någon fil till den armerade installationen. En grön workflow kan inte upphäva R07:s faktiska driftsparitets-NO-GO, äldre watchdog eller kravet på separat fysiskt GO.