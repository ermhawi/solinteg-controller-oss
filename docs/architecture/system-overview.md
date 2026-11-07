# Systemarkitektur – Solinteg på Dell

**Senast dokumenterat:** 2026-11-07. Diagrammen beskriver den [dokumenterat driftsatta normalvägen](../current-state.md), inte en ny systemdesign eller garanterad fysisk effekt. Git-versionen och den platsanpassade installerade dispatchern skiljer sig åt. Se [kända brister](../known-issues.md).

## Husbatteri: prognos, beslut och Modbus

```mermaid
flowchart TB
    subgraph inputs["Externa prognoser"]
        prices["Spotpriser – elprisetjustnu.se"]
        forecast["Väder och solprognos"]
    end

    subgraph host["Dell: Solinteg"]
        planner["Next.js: planering / DP-optimerare"]
        store[("optimizer_runs och readings – SQLite")]
        live[("live.json")]
        dispatch["Dispatcher: återhämtning, EV/L3 och energiarbiter"]
        poller["Poller: registeravläsning"]
        broker["Modbus-broker: UDS och serialiserad worker"]
        web["Webb: Elpriser och Systemstatus"]
        watchdog["Separat heartbeat-watchdog"]
    end

    inverter["Solinteg-växelriktare – TCP 502"]

    prices --> planner
    forecast --> planner
    live --> planner
    planner --> store
    store --> dispatch
    live --> dispatch
    dispatch -->|"UDS: styrtransaktion"| broker
    poller -->|"UDS: FC03"| broker
    broker <-->|"Modbus TCP: FC03/FC06"| inverter
    poller --> live
    poller --> store
    store --> web
    live --> web
    watchdog -.->|"Felväg: separat direkt TCP-försök"| inverter
```

**Viktiga gränser:** Optimeraren beräknar en plan; dispatchern avgör vad som faktiskt får begäras. Modbus-brokern är normalvägens enda TCP-ägare och serialiserar hela kommandosekvenser. **Styrtransaktionen är inte atomär i växelriktaren:** ett nätavbrott kan lämna delvis skrivna register. Den befintliga watchdoggens direkta återställningsförsök vid fel är inte en verifierad, exklusiv nödfallback. Se [brokerbeslutet](adr-2026-11-04-broker-complete-transactions.md), [känd felväg](adr-2026-11-04-broker-legacy-failure-scope.md) och [drift/rollback](../modbus-broker-transactions-release-2026-11-04.md).

## Bil: separat EVCC-styrning

```mermaid
flowchart TB
    ha["Home Assistant: Volvo SoC / laddstatus"]
    evcc["EVCC / Waveshare – separat RS-485-anslutning"]

    subgraph dell["Dell: EVCC-delsystem"]
        volvo["Volvo HA-läsare"]
        observer["EVCC-observatör – FC03"]
        snapshots[("Lokala statusfiler och EV-historik")]
        settings[("Laddläge, avresetid och revision")]
        prices[("optimizer_runs: prisfönster")]
        controller["EV-priscontroller – systemd-timer"]
        ui["Webb /solinteg/ev"]
    end

    ha --> volvo
    volvo --> snapshots
    observer -->|"FC03 – läsning"| evcc
    observer --> snapshots
    snapshots --> controller
    snapshots --> ui
    ui -->|"Spara användaravsikt"| settings
    settings --> controller
    prices --> controller
    controller -->|"FC10: endast 6 A eller paus, efter säkerhetsgrindar"| evcc
    controller --> snapshots
```

**Ingen gemensam Modbus-broker för laddboxen:** EVCC:s FC03-observatör och enda tillåtna FC10-priscontroller använder den separata Waveshare-vägen och samordnas med sitt eget lås och pausägarskap. Webb-API:t lagrar *avsikt*, aldrig ett direkt FC10-kommando. Priscontrollern kan avstå från skrivning om SoC, kabelspårning, Smart Charging-status, nät-/fassituation eller ägarskap är osäkert. Faktisk uppmätt elförbrukning och uppskattad Volvo-energi är inte likvärdiga. Se [EVCC-livebeslut](adr-2026-11-04-ev-price-empirical-live.md), [webbinställningar](adr-2026-11-05-ev-web-live-mode-selection.md) och [empirisk energimodell](../ev-ac-soc-empirical-2026-11-04.md).

## Ansvars- och säkerhetsgränser

| Komponent | Får ändra | Får inte förutsättas |
| --- | --- | --- |
| Webb / API | Laddavsikt och presentationsdata | Direkt inverter-FC06 eller EVCC-FC10 |
| Optimerare | Ekonomiskt planförslag | Fysisk säkerhet eller verkställd laddning |
| Dispatcher | Godkända börvärden via broker | Absolut 20 %-BMS-golv eller verklig nollförbrukning i EMS |
| Broker | Tillåtna Modbus-transaktioner, serialiserade i normalvägen | Hårdvaruatomäritet eller garanterad fallback |
| EV-priscontroller | Verifierad EVCC-paus/återupptagning inom 6 A | Automatisk återstart efter manuellt eller oklart stopp |
| Poller/observatörer | Färska läsningar och historik | Fysisk energikällespårning eller certifierat fasöverströmsskydd |

För hur prioriterings- och prisbesluten tas, fortsätt med [Styrflöden och felhantering](control-flows.md). För tidsstämplad runtimeinventering, se [Aktuellt driftläge](../current-state.md).
