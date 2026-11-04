# ADR 2026-11-04: Driftsätt EVCC-prisstyrning med uttryckligt accepterad empirisk energimodell

**Status:** **Driftsatt på Dell**, 2026-11-04, på grenen `Sundsvall`.
Automatisk EVCC 6 A/prispaus via existerande Waveshare-länk och
`solinteg-ev-price-controller.timer` (**enabled/active**, varje minut).
`solinteg-dispatch`, `solinteg-poller`, Volvo-statusinsamling och
befintlig EVCC-observatör fortsätter separat, utan nya watchdogs.
All prisläsning sker från befintliga husoptimerarens `optimizer_runs`.

## Operatörens uttryckliga avsteg från tidigare GO-grind

Användaren har 2026-11-04 uttryckligen beslutat att den empiriska
AC-kWh/SoC-faktorn duger för **automatisk** EVCC-prisstyrning och
att bristen ska dokumenteras i stället för att blockera.
Detta ändrar **acceptanskriteriet för planeringsenergi**,
inte laddboxens, nätets, fasernas eller pausägarskapets skydd.

`scripts/tools/ev_ac_soc_empirical.py` har under en kontinuerlig
6 A-laddning fått 18 Volvo-molnrapporterade SoC-nivåer, 49–66 %,
med **0,8902 kWh per SoC-procentenhet** genom linjär regression
(R² = 0,99798). Detta är **inte** en oberoende energimätarkalibrering:
EVCC mäter endast Ict2-ström, 230 V och PF=1 antas, och Volvos
SoC-tidsstämplar är API-ändringstider, inte verifierade fysiska
BMS-provtider. Inte heller nyttig batterikapacitet eller
AC/DC-förluster kan särskiljas exakt. Se
[empirisk mätrevision](../ev-ac-soc-empirical-2026-11-04.md).

Det separata `EV_PRICE_ACCEPT_EMPIRICAL_ENERGY=1` väljer ett
**explicit riskaccepterat alternativ** till den strikta,
fortsatt oförändrade `read_ev_session()`-valideringen.
Kontrollkoden anger alltid:
`energy_quality=operator_accepted_evcc_ict2_230v_pf1_cloud_soc_v1`,
`physical_sample_verified=false`,
`independent_ac_meter_verified=false`.
Den sätter **inte** `strict-session.json` till giltig, och
skriver aldrig `independent_ac_calibration` eller
`verified_physical_sample` för dessa källor.
Volvos SoC/anslutning/mål hämtas från det befintliga,
tidskontrollerade HA Energy API-aggregatet. API-mottagning
och lokal status krävs färska inom fem minuter; senaste
SoC-ändring får vara högst tolv timmar gammal. Saknas
underlag är beslutet **ingen ny FC10**.

## Fysisk acceptans före aktivering

1. Förprov med färsk `C2/100/100`, Ict2 cirka 6 A,
   kabelspåraren `connected`, explicit Volvo Smart Charging
   `off`, samt nät/L3 i balanserat läge och under
   konservativa mjukgränser (3 000 W L3, 6 000 W total
   nätimport) passerade utan skrivning.
2. Det **första** manuellt godkända, kontrollerade FC10-provet
   slutade med `EvccUncertainWrite`. En oberoende FC03-läsning
   fastställde `B2 / configured=1000 / pilot=267`; fysisk
   prispaus hade inträffat. Den tidigare 6 A-inställningen
   återställdes efter en *ny* verifiering av exakt fysisk
   status och aktuell nätbudget. Ny FC03 verifierade
   `C2/100/100` med cirka 6 A.
   Exakt felsteg bakom den första osäkra kvitteringen
   fastställdes **inte** och noteras som känd brist.
3. Drivrutinen fick därefter högst nio sekunders **FC03-only**
   väntan på ett fördröjt kontaktor-/CP-läge, med **en enda
   FC10 per åtgärd** och ingen blind retry. Andra fysiska
   provet gav verifierat `B2/1000/267` med låg ström,
   följt av `B2/100/100`. Efterprov med direkt FC03
   verifierade `C2/100/100` och 6,1 A.
   Inga ändringar gjordes av säkringar, amperegräns,
   Solinteg-inverter, bil eller laddboxens hårdvarukonfiguration.
4. Koden installerades först med samtliga fysiska skrivflaggor
   satta till `0`, kontrollerades under systemd som
   `state=disabled`, och armerades **först efter**
   verifierad fysisk återstart och rollbackbackup.
   Första aktiva servicekörningen svarade
   `state=no_change, writes=0`.
   Därefter bekräftade senare minutkörningar fortsatt
   `state=no_change, writes=0` utan fel.

Fysisk acceptans sparad i **privat**, rootägd
`/var/lib/solinteg-evshadow-rollback/ev-price-live-20261104/physical-qualification.json`,
med checksumme- och återställningsmanifest i samma katalog
(mode 0700). Faktisk installation har nätverksbegränsning
till **den privat konfigurerade laddboxadressen**, inte
mallens syntetiska exempel-IP.

## Ägarmodell, tillåtna handlingar och drift

- **6 A eller paus, inget annat.** Schemalagd FC10 kan endast
  skriva `100` (6 A) eller `1000` (prispaus);
  föregående EVCC-status och fysisk läsback via FC03 måste
  överensstämma. 6 A är både aktiverad gräns och maximalt
  automatiskt strömbörvärde. Ingen automatisk strömhöjning.
- Varje minut beräknas elprisbeslut med samma köppriskurva som
  husoptimeraren. `economy` väljer billiga fönster;
  `deadline` prioriterar att försöka uppnå 80 % vid den
  **låsta** avresetiden. Bilens 80 %-gräns i Volvo
  förblir oberoende skydd.
- Den privata sessionsposten
  `/opt/solinteg/ev-shadow-runtime/var/ev-price-empirical-session.json`
  (mode 0600) låser `session_id`, `connection_epoch`,
  `tracking_id`, läge och absolut avresetid.
  En **verifierad fysisk ur-/återanslutning** kan skapa
  ny mission endast när EVCC själv redan kör `C2/100/6 A`
  och ingen tidigare aktiv/terminal ägarpost riskerar
  att ärvas. En `B2/1000`-paus från förra kabelsessionen
  får **aldrig** återstartas automatiskt.
- Prispausägarskap enligt
  `ev_price_pause_owner.py` v2: `pending` före FC10,
  `owned` först efter `B2/1000/267`, `releasing` före
  återstart, `released` vid verifierad läsback.
  Ändrad kabel-, observations- eller sessionsidentitet
  inhiberar/återkallar återstart. `pending`, `releasing`
  och `revoked` fordrar kontrollerad avstämning.
- Volvos Smart Charging-/schemainställningar var avstängda
  vid start. Den existerande, oberoende och skrivskyddade
  Smart Charging-`off`-spärren ligger kvar. Ingen stoppknapp
  för hemmaladdning finns i den aktuella Volvo Cars-appen.
- L3 och sammanlagd nätimport verifieras vid **återstart**,
  plus att husets ordinarie dispatcher hanterar
  batteriprioritet. Mjukgränserna är inte ett substitut
  för fysisk säkring eller oberoende mätt RMS-fasström.

Vid aktiveringen var SoC cirka 66 %; kvar till 80 %
cirka **12,46 kWh enligt estimatet**. Det skulle kräva
cirka **9 timmar** med 6 A, trots bara cirka 8,3 timmar
till avresa 07:00 den 5 november. Policyn valde
**tidskritisk fortsatt 6 A-laddning**, inte prispaus;
80 % kan **inte garanteras** till denna avresa.

## Kända brister och risker som accepteras eller kvarstår

1. **Accepterad planeringsosäkerhet:** ingen separat
   EM111-kWh, ingen verifierad BMS-provtid, ± minst
   SoC-kvantiserings-, spännings- och PF-bias; felaktigt
   beräknad kvarvarande laddtid/energibudget kan ge
   missat 80 %-mål eller sämre ekonomiskt utfall.
   R² är **inte** osäkerhetsmått för energimätning.
2. **Fysisk sessionskontinuitet:** EVCC:s ungefär
   30-sekunders polling kan missa en mycket snabb
   ur-/inkoppling. `coverage_epoch` och
   `tracking_id` skyddar mot upptäckta luckor och
   återskapade filer, **inte förlustfri** detektion.
3. **Terminal paus/kommunikationsavbrott:** en
   osäker FC10-kvittens lämnar `pending` eller
   `releasing`; frånkoppling, ändrad session, manuell
   EVCC-manöver eller källa som försvinner kan lämna
   ägarpost `revoked`. **Automatisk återstart sker
   då inte**, även om priset blir billigt. En pausad
   laddbox kan därför förbli pausad tills kontrollerad
   manuell återställning utförts.
4. **Avresetid och källdata:** tidsfristen låses
   per kabelsession. När den har passerat stoppas
   **nya** beslut i stället för att i tysthet förlänga
   uppdraget ett dygn. HA:s 5-minuters- och
   12-timmarsgränser kan stoppa automatiken vid
   källbortfall.
5. **Första fysiska kvitteringsanomalin:** orsak
   inte fullständigt isolerad. Bunden efterkontroll
   löste andra provet; återkommer den gäller
   samma osäkert-utfall- och ingen-retry-policy.
6. **Visningen `Bil · prisfönster`:** diagrammet
   är fortfarande förhandsvisning av prisplanen;
   dess etikett är inte i sig bevis för EVCC-kommandon.
   Bekräfta verkliga beslut i systemd-journalen.

## Kontroller, återställning och artefakter

```sh
systemctl status solinteg-ev-price-controller.timer --no-pager
sudo journalctl -u solinteg-ev-price-controller.service -n 20 --no-pager
systemctl status solinteg-ev-observer.service solinteg-dispatch.service solinteg-poller.service --no-pager
```

**Säker rollback kräver först fysisk statuskontroll.** Att stoppa
timern återställer **inte** automatiskt `PWM=1000` till 6 A:

```sh
sudo systemctl disable --now solinteg-ev-price-controller.timer
sudo systemctl stop solinteg-ev-price-controller.service
```

Läs sedan färsk fysisk FC03 och den privata pausägarposten.
Om `B2/1000/267` råder, ge **inte** blind FC10: verifiera
säkringsmarginal, kabelkontinuitet och aktuell manuell
laddavsikt. En särskilt godkänd, låst, readback-verifierad
återställning till **samma 6 A** krävs innan anläggningen
kan betraktas som normal; vid osäkert svar stoppa och
undersök igen. Efter att fysisk `C2/100/6 A` är
verifierad, återställ skrivflaggornas tidigare
**disabled**-version från den privata backupen under
`ev-price-live-20261104/` och kontrollera
`enabled/active` för oförändrade dispatcher,
poller, EVCC-observatör och Volvo-status-timer.

Privat rollbackmanifest innehåller exakt vilka
skyddade källfiler som skapades, tidigare
`ev_price_policy.py`, första versionerna av fysisk
test-/driverkod, samt kopia av env-filen före aktivering.
Bevara privata `ev-price-pause-owner.json`-tombstones,
sessionspost, kvalificeringsbevis och backup för
återställning/revision. Inga extra watchdogs,
shadow-processer eller automatiska rensningstimrar
tillkom; ingen agentägd one-off-process är kvar.
