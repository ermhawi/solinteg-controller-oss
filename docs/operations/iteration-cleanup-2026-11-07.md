# Z04 · Ägarverifierad tillfällig teststädning

**2026-11-07 – `source/tested`, endast inaktiva temporära sessionfiler.** Operatörens tidigare Git-arbete, worktrees, /opt-installation, broker, EVCC och skyddade återställningsunderlag lämnades orörda.

- Efter den fulla regressionen gav läsande `ps -eo` **0 pågående testprocesser** som matchade isolerade Python-unittest-, Vitest- eller Next-build-kommandon. Ingen process har stoppats.
- **24 explicit namnlistade och av denna arbetsomgång skapade `/tmp/solinteg-...`-loggar och mellanrapporter** togs bort först efter kontroll av `lstat`, vanlig fil, ingen symlänk och ägarskap `uid==os.getuid()`. Tre äldre/nya lokala inventeringsmellanrapporter under `/tmp` fanns bland dem. Inga andra filer eller globala matchningar raderades.
- Det **committade** och sanerade källbeviset (inklusive 83-modulers Python-inventering, 86-modulers TypeScript-audit, test/ADR/runbooks) och de **privata** beständiga snapshotarna under `~/.local/state/solinteg-release-staging` och `~/.local/state/solinteg-release-inventory` **bevarades**. Inga backup-, status-, ägar-/kabelepok- eller recovery-markers ändrades.
- Läsande `systemctl show`: `solinteg-broker-control.service`, `solinteg-dispatch.service`, `solinteg-ev-price-controller.timer` och `wifi-recovery-watchdog.timer` var **loaded/active** efter städningen; `solinteg-hold-probe.service` **not-found/inactive**. Inte en enda systemd-unit installerades/stoppades/återstartades.
- Git: **14 tidigare lokala modifikationer** skyddas enligt B04. Ingen stash eller worktree som tillhör någon annan har raderats. Slutlig Git-hygien är fortfarande Z03, separat från denna säkra städning.

**Begränsning:** Statusen gäller just de namngivna testerna och systemd-enheterna vid lästillfället. Ett arkiverat eller okänt operatörsscript kan vara relevant för återställning även när dess unit är `not-found`; därför gäller fortsatt [BEHÅLL/FRYS](probe-quarantine-2026-11-07.md) och [historisk unit-inventering](legacy-units-inventory-2026-11-07.md).
