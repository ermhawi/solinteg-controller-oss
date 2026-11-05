# ADR 2026-11-05: Två EVCC-laddlägen styrs via Solintegs webbsida

**Status: driftsatt på Dell**, befintlig `solinteg-web.service`
och `solinteg-ev-price-controller.timer`, gren `Sundsvall`.
Detta är en efterföljande konkretisering av
[EVCC-prisdriftsättningen](adr-2026-11-04-ev-price-empirical-live.md).
Ingen ändring av husbatteriets dispatcher, Modbus-broker, fysisk
6 A-gräns, Volvo-appens schema, växelriktarens EMS eller
laddboxens hårdvarukonfiguration.

## Operatörens beslut och avsedd funktion

Via den befintliga sidan **`/solinteg/ev` / Smart laddplanering**
väljer användaren ett av två lägen:

1. **Klar till avresa (`deadline`)**: försök nå Volvos 80 %-mål
   vid angivet lokalt klockslag i **Europe/Stockholm**, med
   prisoptimering när marginalen räcker och annars så mycket
   6 A-laddning som behövs och kan ske säkert. Det är
   **inte** garanterat möjligt att nå 80 % i tid.
2. **Ladda ekonomiskt (`economy`)**: välj billiga
   elpriskvartar utan sluttid, med valfritt absolut tak i
   **öre/kWh totalt köppris**. Bilens mål förblir 80 %.

Båda använder samma ekonomiska plan och separata 6 A-/EVCC-
ägar- och L3-skydd. Att växla formulärknapp ger **ingen
direkt FC10**; ett autentiserat tryck på **Spara laddläge**
skriver endast den nya lokala avsikten.

## Upptäckt kod-/driftavvikelse före ändringen

`app/ev/page.tsx`, `EvChargingPlanner.tsx` och
`app/api/ev/*` fanns i Git men inte i den installerade
Next.js-applikationen: den returnerade HTTP 404 för
`/solinteg/ev` och `/solinteg/api/ev`.
Git-UI:t märkte sidan felaktigt med **SKUGGLÄGE · 0 skrivningar**,
trots den redan aktiva EVCC-priscontrollern.
Den tidigare inställningsfilen
`/opt/solinteg/ev-shadow-runtime/var/settings.json`
ägdes av root och dess katalog var inte skrivbar
för webbanvändaren. Att ge webben generell skrivrätt
till den katalogen hade riskerat EVCC:s ägarfiler
och är uttryckligen **inte** lösningen.

## Design: autentiserad avsikt, inte hårdvaru-API

En dedikerad katalog
`/opt/solinteg/ev-charge-settings/` har skapats med
ägare root, grupp `solinteg`, mode **2770**.
`settings.json` är mode **0640** och är den **enda
inställningsfil** som både webben, HA-läsaren och
priscontrollern nu använder. Tidigare
`deadline/07:00` har förts över byteidentiskt.
Webbens systemd-miljö får via en privat
`/etc/solinteg/ev-web-settings.env` (root **0600**)
exakt `EV_SETTINGS_PATH`, HA-statusens och EVCC-
observatörens skrivskyddade sökvägar samt två
uttryckliga opt-in-flaggor.

HTTP **PUT `/solinteg/api/ev/settings`** är förvalt
stängt och kräver ett långt, separat, lokalt
administratörs-Bearer-värde som kontrolleras med
konstanttidsjämförelse. Inget lösenord, VIN,
fysisk laddboxadress eller privat env-värde
finns i Git, HTML eller läsbart GET-svar.
Skriv-API:t avvisar okända JSON-fält, invalid
tid/pristak, en gammal `revision` från en annan
flik och samtidiga filskrivningar. Servern
genererar ett nytt slumpmässigt UUID endast när
laddavsikten faktiskt ändras; identiska värden
orsakar ingen ny mission. Filen byts atomiskt,
fsynkas och är fortsatt grupp-läsbar för HA och
priscontrollern. Inget webb-API sänder FC10.

Priscontrollern publicerar sin **sanerade**
state/owner/proveniens-sammanfattning en gång per
minut som 0640-fil
`ev-price-controller-status.json` i befintliga
skyddade var-katalogen. Den **privata** pausägarfilen
förblir 0600, oåtkomlig från webbprocessen.
Spar-API:t kräver högst 125 sekunder gammal
och aktiv controllerstatus med `owner_state=none|released`,
samt färsk FC03 som visar redan körande
`C2/PWM100/6 A` eller ofarligt stillastående
`A1/B1/B2` med fysisk ström nära 0 och
**inte** ägd prispaus. `B2/PWM1000`,
`pending`, `owned`, `releasing`, `revoked`
eller okänd kabelstatus ger **409**, inte en
smygstart eller överförd återstartsrätt.
Sidan visar senaste effektiva läge, låst avresetid
och senaste beslut separat från det sparade valet.

## Sessionsfäktning vid byte av läge eller klockslag

Den uttryckliga nya `revision` valideras av både
TypeScript och Python `ha_ev_session.load_settings`.
För tidigare inställningar utan revision gäller
den gamla kompatibla sessionsidentiteten.
Efter sparande av en **verklig ändring** bildar
`ev_price_empirical_session.read_session()` ny
sessions-UUID av bilidentitetens hash,
kabelspårar-ID, kabel-epoch, laddläge och
inställningsrevision. Tidigare `departure_at_utc`
får inte återanvändas när klockslag ändras.

En ny avsikt kan ersätta den beständiga missionen
under **samma** kabelsession endast när
verifierad FC03 är **C2/100/6 A** och det
inte finns ett tidigare ägt, osäkert eller
återkallat prispausläge. Ett bekräftat
ur-/inkopplingsförfarande fortsätter tillåta
separat ny mission som tidigare. Pågående
`B2/1000`-prispaus är fortfarande **inte**
automatisk återstartbar vid modebyte.
Om HA ännu inte har publicerat det nya
inställningsvalet misslyckas käll-matchningen
och priscontrollern gör **ingen FC10**.
Strax före varje fysisk FC10 jämför controllern
också färska inställningar (revision/läge/
avresetid/pristak) med det beslut som just
beräknats; ändrade värden avbryter skrivningen.
`deadline` låses som absolut UTC för varje ny
mission och svensk sommar-/vintertid hanteras
av den tidigare verifierade `next_departure()`.

## Driftsättning och verifiering

Webben byggdes från **exakt den installerade**
Next.js-basen i separat agentägd byggkatalog med
endast sju avgränsade Git-källfiler tillagda/
uppdaterade, inklusive en länk i `AppShell`.
Det undvek att rulla ut orelaterade ändringar
från den permanenta utvecklingsgrenen.
Next.js 16.2.10 (Turbopack) byggde både
`/ev` och `/api/ev{,/settings}`.
Endast `solinteg-web.service` stoppades för
atomärt byte av `.next` och startades igen.
Dess gamla bundle och föreliggande källfiler
bevaras i privata återställningskatalogen.

Verifierat på Dell efter driftsättningen:
- `GET /solinteg/ev`: **200**.
- `GET /solinteg/api/ev`: **200**, `settingsWriteEnabled=true`,
  `liveControlConfigured=true`, färsk aktiv controller
  och `ownerState=none`.
- `PUT /solinteg/api/ev/settings` utan nyckel: **403**.
- PUT med korrekt nyckel men **oförändrade**
  `deadline/07:00`: **200**, `mode=live`,
  `deviceWrites=0`, identisk inställningsfil.
- Priscontroller: `state=no_change, writes=0`,
  befintlig `deadline/07:00`, samma
  `departure_at_utc`; EVCC `C2/PWM100/6 A`.
- Varken några nya systemd-timers, watchdogs eller
  kommandoverktyg för hårdvarustyrning har införts.

**Viktigt:** Ett **verkligt** byte till
`economy` eller ett annat klockslag har
inte utförts i produktion under verifieringen,
eftersom operatören inte valt något nytt
laddläge. Behörig användare kan nu göra det
på sidan. Automatiska tester täcker ändrad
klockslag/mode, låst tid, samtidig uppdatering,
prisägd paus och utebliven äldre sessions-
återstart. Fysisk FC10 vid billigt/dyrt
prisskifte verifierades i föregående ADR,
inte på nytt i denna webbändring.

## Återställning, drift och begränsningar

Privat, rootägd rollback och manifest:
`/var/lib/solinteg-evshadow-rollback/ev-web-settings-20261105/`
(**0700**), med byteexakt kopia av tidigare
HA-/priscontrollerkod, privata env-filer,
tidigare settingsfil, tidigare live-Webb-Next
bundle samt tidigare redan installerade
webbkällfiler. Behåll dessa tills offsitearkiv
har verifierats med tidigare föreskriven
krypterad USB-backup och återställningsprov.

För att spärra **nya** webbändringar utan
att påverka befintlig EVCC-pristimer: ta bort
eller stäng av webbens privata
`EV_SHADOW_SETTINGS_WRITES_ENABLED=1` och
starta om **enbart** `solinteg-web`.
För full rollback: säkra de aktuella
fysiska EVCC-registren först, bevara alla
prispausägartombstones, återställ tidigare
Git-/env-/Next-versioner från manifest,
kör `systemctl daemon-reload` och
starta om berörda webb-/HA-läsartjänster.
Ändra inte själva laddboxens PWM genom
en webbrullback: ett separat ägt,
fysiskt verifierat återställningsförfarande
krävs om `PWM1000` råder.

**Kvarstående brister:** Den empiriska
faktorn 0,8902 kWh per SoC-procentenhet
är fortfarande **inte** oberoende kalibrerad;
Volvo-SoC har inte fysiska BMS-provtider;
mycket snabb ur-/inkoppling kan missas.
Ändring medan en ägd prispaus pågår
måste invänta verifierad återstart/
kontrollerad avstämning. 80 % till
avsatt tid kan inte garanteras vid
för låg tillåten ström eller för kort
tidsmarginal. Detta är uttryckligt
accepterade begränsningar, inte påstådda
säkerhetsgarantier.
