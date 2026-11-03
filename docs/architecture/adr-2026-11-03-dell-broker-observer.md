# ADR 2026-11-03: Dell först för broker-watchdog, endast observation

**Status:** Accepterad för **observerande drift på Dell**, inte för
privilegierad nödfallback eller fysisk EMS→General-styrning.

## Kontext

- Raspberry Pi är en bättre extern fel-domän men har konstaterad
  pågående underspänning och använder Wi-Fi. Den är ännu inte säker
  som automatiskt återställande övervakningsnod.
- Dell driver den befintliga direktpollern, dispatchern och watchdoggen.
  Brokerns läs-/styr-crossover och exklusiva TCP-ägarskap är inte
  genomförda.
- Färsk dispatcher-heartbeat säger att processen lever, **inte** att
  dess senaste registerbörvärde är säkert eller att en broker är aktiv.

## Beslut

- Kör en separat `solinteg-broker-observer.timer` på Dell var
  15:e sekund. Den kontrollerar lokala dispatcher-/poller-tidsstämplar
  och, först efter en separat read-only-cutover, brokerns `status`
  via skyddad Unix-socket.
- Ingen TCP-åtkomst, inga Modbusregister, inga systemd-stopp, inga
  fysiska fallback-kommandon och ingen privat `.env` i observatören.
  Processen körs som `solinteg` med `NoNewPrivileges`,
  `RestrictAddressFamilies=AF_UNIX`, `IPAddressDeny=any` och
  tom capability-lista.
- En ny, root-ägd fil ligger utanför det installerade appträdet.
  Den ordinarie watchdoggen och driftsättningen lämnas oförändrade.
- Fel klassificeras som `fault` och loggas till journald; status
  `healthy` får inte tolkas som ett frikännande av fysisk styrning.

## Konsekvenser och uppföljning

Dell-lokal observation upptäcker **inte** att hela Dell stängs av
eller tappar ström och kan inte vara den slutliga externa fallbacken.
Flytt till Pi kräver säkrad strömförsörjning, ett autentiserat
heartbeatprotokoll från Dell till Pi och väl verifierad nätverksfencing.

Innan en framtida automatiskt ingripande watchdog får aktiveras
måste exklusiv TCP-ägare, avbrott mitt i flerregistersekvens,
SoC/BMS-aware fallback, fysisk readback, persistent spärr och
rollback vara verifierade. Ett gammalt EMS-börvärde får varken
lämnas ohanterat eller bytas till General av enbart ett timeoutvärde.

**Driftevidens och rollback:** [Dell-observatör](../modbus-broker-watchdog-dell-2026-11-03.md).
**Migrationsplan:** [Modbus-broker](../plans/modbus-broker-2026-11-01.md).
