# Läsande driftverifiering

**Syfte:** kontrollera om Solintegs aktuella drift motsvarar [systemkartan](../architecture/system-overview.md), utan att påverka växelriktare, EVCC eller driftprocesser. Kopiera inte hemliga miljövariabler, privata nätadresser eller hushållets råmätvärden till Git.

## Systemd och styrkedjor

```bash
systemctl is-active solinteg-broker-control.service solinteg-poller.service \
  solinteg-dispatch.service solinteg-web.service
systemctl is-active solinteg-ev-observer.service solinteg-ev-history.service \
  solinteg-ev-ha.service
systemctl show solinteg-dispatch.service -p ActiveState -p DropInPaths
systemctl list-timers --all --no-pager | grep -E 'solinteg-|wifi-recovery-watchdog'
```

**Tolkning:** `active/running` för broker, poller och dispatcher visar **processhälsa**, inte att batteriets begärda watt motsvarar fysisk effekt. Periodiska `oneshot`-tjänster som `solinteg-ev-price-controller.service` kan normalt vara `inactive/dead` *mellan* minuterna även när timern är `active/waiting`.

## Korrelera med verkliga observationer

Kontrollera tidsstämplarna i `live.json`, senaste giltiga `readings`, `optimizer_runs` och `control_actions`, samt EVCC-kabel-/FC03-status. Begärd laddning ska jämföras med **efterföljande registerreadback och fysisk mätning**, inte bara en kontrollrad.

En aktiv timer bevisar inte att EVCC fått ett FC10-kommando, att solöverskott laddat husbatteriet, att EMS har ett fysiskt 20 %-golv eller att watchdog har fungerat vid ett broker-/Wi-Fi-fel. Utför inga extra direkta Modbus-prober; de kan konkurrera med brokern.

## Skilj Git och installerad produktion

```bash
git -C ~/dev/github.com/ermhawi/solinteg-controller-oss status --short --branch
git -C ~/dev/github.com/ermhawi/solinteg-controller-oss worktree list
```

En Git-commit eller fil under `deploy/` är **inte** ett kvitto på driftsättning. `/opt/solinteg/app` innehåller platsanpassad kod som tidigare patchats selektivt. Ny release eller rollback kräver en verifierad installerad SHA-/AST-baslinje och separat godkännande.

## Vid misstänkt fel

Notera tidpunkt och tidszon, mätvärdenas ålder, resultatet för systemd/timers, senaste verifierade läge, brokerfel och osäkra FC06-kvittenser. Markera okända observationer som `okända`. Utför **inte** direkt Modbus-skrivning, automatisk omstart eller återanvändning av SoC25-experiment som del av denna kontroll.

Se [brokertransaktioner och rollback](../modbus-broker-transactions-release-2026-11-04.md), [felväg och watchdog](../architecture/adr-2026-11-04-broker-legacy-failure-scope.md), [styrflödesdiagram](../architecture/control-flows.md) och [kända brister](../known-issues.md).
