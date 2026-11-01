# ADR 2026-11-01: isolerat tredje 25–26→28 %-återhämtningsprov

**Status: fysiskt prov aktiverat, därefter säkerhetsavbrutet och
återställt kl. 21:10 CET 2026-11-01.** SoC 28 % verifierades inte.
Den nya 50-identiteten är terminal och får inte återanvändas.

## Bakgrund

Det första fysiska SoC25-försöket (48) aborterade vid en legitim
26 %-mätning mellan dispatcherkörningarna. Det andra (49) aborterade
på en sannolikt icke-atomär batteri/AC-avläsning under växlingen
mellan EMS och General/Auto. Båda återställde ordinarie dispatcher.
Ingen verifierad återhämtning till 28 % har gjorts.

## Beslut

Skapa **en tredje, oberoende engångsprovprocess** (`50-`), egna
immutable root-filer, root-status, två egna systemd-timers och
60-minuters maxgräns. Tillåt enbart SoC 25–26 % och fristående
EVCC A1 enligt förkontroll; tidigare två identiteter måste förbli
terminala och tidigare planförbikoppling vara exakt återställd.

Återanvänd processlokalt omslag runt den installerade originaldispatchern.
Ingen extra normal Modbus-skrivare, ingen ny BMS-/inverter-SoC-gräns
och ingen EVCC-åtgärd införs. En framtida single-writer broker
driftsätts inte under provet.

Fastställ en enda, snäv tidsundantagsregel: under de första 120 s
efter en dispatcheromstart kan en batteri/AC-avvikelse i General/Auto
avvaktas bara om CT/house/site-balansen fortfarande är verifierad,
SoC 24–28 % och verklig batteriurladdning 0–1500 W.
Maximalt 180 s efter en sådan observation måste växelriktaren
ha börja återhämtningsladda; annars ägarverifierad rollback.
Ingen generell säkerhetskontroll stängs av.

Lyckad laddning fastställs genom två fysiska negativa batteri-W
avläsningar minst 20 s isär under EMS 0x303, och slutlig SoC >=28%.
Båda watchdog-timrar stoppar och återställer root-ägd 50-overlay
vid abort eller lyckad färdigställning.

## Utfall och beslut om fortsättning

Den fysiska 50-som-provet startades 21:09:30 CET vid 25 % SoC
efter godkänd root-förkontroll. Originaldispatchern loggade
`charge 1500W`. Den första pollermätningen under EMS `0x303`
gav `battery_w=-1692`, samtidigt som AC-utgången var +978W,
PV 0W, hus 989W och netto nätimport 11W. Fas/house/AC stämde
matematiskt men batteriets energibalans motsade detta med
2 670W. Säkerhetswatchdoggen stoppade försöket omedelbart,
återställde originaldispatchern, inaktiverade båda timers och
lämnade `state=aborted` i den 50-root-journalen.
En efterföljande mätning visade 25 % och 233W urladdning.
Ingen 28 %-verifiering eller två konsistenta laddningsprover
registrerades.

Vi **vidgar inte** säkerhetsundantaget till dessa mätvärden
under samma godkännande. Ingen ny Modbus-skrivare eller nytt
fysiskt prov ska startas för att kompensera en motstridig
energibalans. Nästa ändring bör först klargöra icke-atomära
Modbus-telemetriögonblick och etablera förbättrad
en-skrivare/serialisering via Modbus-broker. Det är ett
separat tekniskt och fysiskt ändringsbeslut.

## Konsekvenser

Ett lyckat enskilt laddprov bevisar inte permanent reservstyrning
vid upprepade cykler. Originaloptimeraren får efter återställning
åter styra ekonomisk laddning/urladdning. Native BMS/fuse är sista
säkerhetsnivå; 30 s Modbus/CT-sampling är inte kontinuerlig
hårdvarusäkring. Det tredje försöket är engångsstatus och kan inte
startas om genom att återanvända status eller timers.

Se [runbook](../soc25-recovery-third-2026-11-01.md).
