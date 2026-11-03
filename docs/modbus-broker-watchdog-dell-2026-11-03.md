# Modbus-broker: Dell-baserad säkerhetsobservatör (2026-11-03)

**Driftstatus:** Installerad på Dell, **endast observation**. Det finns en
[separat offlinegrind för framtida privilegierad nödfallback](modbus-broker-emergency-policy-offline-2026-11-03.md);
den är **inte** kopplad till denna tjänst. Den ordinarie
pollern, dispatchern, webbappen och den befintliga `solinteg-watchdog.timer`
kör oförändrat. Modbus-brokern är **inte driftsatt** och den nya observatören
är **inte en privilegierad nödfallback**.

**Arkitekturbeslut:** [Dell-först, Pi-senare](architecture/adr-2026-11-03-dell-broker-observer.md).

**Avgränsning:** `scripts/services/modbus_broker_watchdog_observer.py`,
`deploy/solinteg-broker-observer.service` och
`deploy/solinteg-broker-observer.timer` är fristående från de vanliga
Solinteg-tjänsterna. Ingen kopiering av utvecklingsbranchens övriga
källkod till produktion har utförts.

## Övervakning

- `systemd` anropar en liten, separat one-shot-process var **15:e sekund**
  efter tjänstens föregående avslut, utan krav på att dispatchern kör.
- Dispatcherns heartbeat måste ha en tidszonsmedveten tidsstämpel
  mellan **0 och 180 sekunder** gammal; pollerns `live.json` mellan
  **0 och 120 sekunder**. Framtida, saknade, trasiga eller naiva
  tidsstämplar räknas inte som friska.
- `supervisor_required=true`, `broker_policy=inhibit` eller okänt
  brokerläge ger `fault` även när heartbeat är färskt.
- `BROKER_OBSERVER_REQUIRE_BROKER=0` är den **installerade** defaulten:
  ingen broker krävs eller kontaktas, eftersom denna ännu inte
  körs. Efter separat godkänd read-only-cutover kan denna flagga
  sättas till `1` så att enbart `status` hämtas via AF_UNIX.
  Då krävs read-only broker, disarmed, ingen safety latch,
  färskt sista broker-sample och kvalitetsmärkt pollerdata.
- Vid fel skrivs en statuspost `fault` och ett varningsmeddelande
  i journald, med påminnelse högst var 15:e minut. Denna version
  skickar **inga push-larm och inga registerkommandon**.
- Statusfilen uppdateras vid lägesbyte och annars högst varje minut.
  `healthy` betyder endast att övervakade signaler är färska,
  **inte att växelriktarens fysiska läge eller SoC är säkert**.

## Säkert installerad på Dell

| Komponent | Plats |
| --- | --- |
| Root-ägd, fristående kod | `/opt/solinteg/safety/modbus_broker_watchdog_observer.py` |
| Systemd service | `/etc/systemd/system/solinteg-broker-observer.service` |
| Systemd timer | `/etc/systemd/system/solinteg-broker-observer.timer` |
| Egen observerstatus | `/var/lib/solinteg-broker-observer/status.json` |

Service körs som `solinteg` med `NoNewPrivileges`,
`ProtectSystem=strict`, `RestrictAddressFamilies=AF_UNIX`,
`IPAddressDeny=any`, tom capability-lista och
`StateDirectoryMode=0700`. Ingen `EnvironmentFile` med privata
Solinteg-hemligheter läses och inga systemd-stoppsignaler skickas.
Övriga installerade tjänster och deras aktiva kodfiler lämnas orörda.

### Verifiera drift

```bash
systemctl list-timers solinteg-broker-observer.timer
systemctl status solinteg-broker-observer.timer
journalctl -u solinteg-broker-observer.service -n 40 --no-pager
sudo cat /var/lib/solinteg-broker-observer/status.json
```

**Initial kontroll 2026-11-03:** `healthy`, tom `reasons`,
`mode=observe`, `broker_required=false`, `physical_writes=false`.
De installerade `solinteg-dispatch`, `solinteg-poller`,
`solinteg-web` och den gamla watchdog-timern var aktiva.
Källkodens dispatcher/poller är **inte** samma revision som
installationen; en synkning ingår inte i denna ändring.

### Återställning

```bash
sudo systemctl disable --now solinteg-broker-observer.timer
sudo systemctl stop solinteg-broker-observer.service
```

Detta lämnar de ordinarie tjänsterna, den befintliga watchdoggen
och växelriktaren helt orörda. Vid eventuell senare avinstallation
ska bara de fyra ovan angivna agentägda filerna/katalogerna
inventeras och tas bort efter bekräftad inaktivitet. Inget
destruktivt städjobb är installerat.

## Flytt till Raspberry Pi och återstående skydd

Koden har inga Dell-specifika API:er, men Pi:n kan **inte bara ta över
systemd-enheten**: `dispatch-heartbeat.json`, `live.json` och
broker-UDS finns lokalt på Dell. Extern observation kräver en
autentiserad, återspelningsskyddad signal och validerad färskhet.
Pi:ns pågående underspänning måste också vara löst och helst Ethernet
anslutet innan den får fungera som säkerhetsnod.

**Viktig lucka:** Observatören på samma värd upptäcker inte Dells
totala strömavbrott. Inget exklusivt TCP:502-ägarskap, fysisk
fencing, säker EMS→General-övergång, BMS-/SoC-grind eller
register-readback har installerats. Den befintliga watchdoggen kan
fortfarande göra sin tidigare direkta General-återgång; den nya
observatören påverkar den inte. Fysisk broker-cutover: **NO-GO**.
