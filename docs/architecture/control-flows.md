# Styrflöden – prioritet, EVCC och felvägar

**Tolkning:** Diagrammen förenklar styrbeslut för att visa **ordning och ansvar**. Den faktiska koden och de dokumenterade driftsbesluten är normerande. Diagrammen utgör varken en certifierad elsäkerhetsanalys eller en komplett formell specifikation.

## Batteriets dispatchkedja

```mermaid
flowchart TD
    start(["Ny dispatcheriteration"]) --> inputs["Läs plan, SoC, poller och EVCC-status"]
    inputs --> quality{"Tillräckligt färska och konsistenta uppgifter?"}
    quality -->|"Nej / osäkert"| uncertain["Avstå från ny overifierad sol-/EV-omstyrning; befintlig felpolicy gäller"]
    quality -->|"Ja"| proposal["Optimerarens ekonomiska förslag"]
    proposal --> recovery["SoC-återhämtning har företräde vid fysisk nödnivå"]
    recovery --> ev["EV/L3: minska oönskad batteriurladdning mot laddande bil"]
    ev --> arbiter["Energiprioritets-arbiter: solfångst kontra EMS-sparläge under reserv"]
    arbiter --> phase["Befintliga ladd-/fas-/nätgränser samt övriga säkerhetsspärrar"]
    phase --> decision{"Åtgärden godkänns?"}
    decision -->|"Nej"| reject["Behåll säker fel-/avvisningsväg"]
    decision -->|"Ja"| broker["Broker: en tillåten serialiserad styrtransaktion"]
    broker --> feedback["Verifiera readback och efterföljande fysisk telemetri"]
```

Detta är **prioritering av styrförslag**, inte en algoritm som kan styra eller bevisa enskilda elektronflöden. Återhämtning prioriteras före prisväntan, och en pågående riktig laddning får inte ersättas av ekonomiskt sparläge. Under den mjuka **20 %-reserven** kan två separata, tidsmässigt konsekventa solöverskottsprover låta General ersätta EMS 50 W; hysteresis och urladdningsbudget hindrar snabb växling tillbaka. Ovanför reservnivån styr befintlig optimerare ekonomin. Den här arbiter-versionen är **inte** en generell ersättare för alla optimeringssteg. Se [fullständig prioritet och tröskelvärden](adr-2026-11-05-energy-priority-arbiter.md).

### Fysisk säkerhet kontra mjuk ekonomi

| Regel | Verifierbar innebörd | Begränsning |
| --- | --- | --- |
| Fysisk SoC-recovery | Särskild återhämtning vid låg SoC | Ladd-/fasskydd gäller även då |
| EV grid-first | Minska batteriets bidrag till bilens last när mätningen räcker | Separat EVCC-Ict2 kan inte visa exakt källa till varje watt |
| Solupptagning | Undvik onödig export och EMS/General-fladder under låg reserv | General kan fortfarande urladda innan nästa reglering |
| Batterisparläge | Ekonomiskt avstå från större uttag vid låg reserv | EMS-börvärde 50 W är **inte** 0 W |
| EndSOC och BMS | Fysiska skydd måste vara sista barriär | Prioritet hos native EndSOC i EMS BattCtrl är ännu **inte verifierad** |

## EVCC:s automatiska prisstyrning

```mermaid
flowchart TD
    tick(["Priskontroller: varje minut"]) --> fresh{"Färsk Volvo-/EVCC-/kabel-/nätstatus och giltig session?"}
    fresh -->|"Nej"| inhibit["Inhibited: ingen FC10"]
    fresh -->|"Ja"| plan["Läs prisfönster och sparat läge – deadline eller economy"]
    plan --> change{"Behövs en fysisk ändring?"}
    change -->|"Nej"| noop["No change: ingen FC10"]
    change -->|"Pausa"| pause{"Verifierat C2, 6 A och säkert nytt pausägarskap?"}
    change -->|"Återuppta"| resume{"Egen verifierad prispaus, oförändrad revision och kabelsession?"}
    pause -->|"Nej"| inhibit
    resume -->|"Nej"| inhibit
    pause -->|"Ja"| recheck["Omkontroll av säkerhetsgrindar och inställningsrevision"]
    resume -->|"Ja"| recheck
    recheck -->|"Godkänd"| fc10["Exakt en FC10: paus eller högst 6 A"]
    recheck -->|"Osäker"| inhibit
    fc10 --> readback{"FC03-readback / fysisk status verifierad?"}
    readback -->|"Ja"| record["Registrera tillstånd och ägarskap"]
    readback -->|"Nej / timeout"| fail["Osäkert utfall: ingen blind retry eller automatisk återstart"]
```

**Viktiga konsekvenser:** Att trycka **Spara laddläge** i webbgränssnittet ändrar endast beständig avsikt. Webb-API:t skickar ingen FC10. En gammal prispaus får inte ärvas av en ny laddmission eller en ändrad avresetid. Smart Charging-status från Volvo måste vara entydigt avstängd för ny prisstyrning. Utan färsk sessions- och kabelkontinuitet är **ingen skrivning** det avsedda konservativa resultatet. Se [laddlägen och revisionsskydd](adr-2026-11-05-ev-web-live-mode-selection.md) och [verifierad prisaktivering](adr-2026-11-04-ev-price-empirical-live.md).

## Brokerfel och återställning

```mermaid
sequenceDiagram
    participant D as Dispatcher
    participant B as Modbus-broker
    participant I as Växelriktare
    participant W as Befintlig watchdog

    D->>B: UDS: transaction_charge / discharge / auto
    B->>I: Serialiserade FC03 och FC06
    alt Hel sekvens + readback lyckas
        I-->>B: Verifierade registersvar
        B-->>D: Resultat
    else Förlorad kontakt eller oklart FC06-svar
        B-->>D: Fel / okänt utfall
        Note over B,I: Tidigare registersteg kan redan vara utförda
        D->>B: Befintligt återställningsförsök
        opt Om dispatcher eller broker slutar fungera
            W->>I: Direkt TCP: försök till General / Auto
            Note over W,I: Kan misslyckas eller konkurrera med broker-TCP
        end
    end
```

Den gamla watchdoggen saknar idag verifierad fencing och fullständig oberoende återställning för alla fel. **Ingen av pilarna vid fel är en garanterad fysisk återgång till Auto.** Se [accepterad felvägsrisk](adr-2026-11-04-broker-legacy-failure-scope.md), [transaktionernas begränsningar](adr-2026-11-04-broker-complete-transactions.md) och [kända brister](../known-issues.md).

[Systemets komponentkarta](system-overview.md) · [Aktuell driftstatus](../current-state.md).
