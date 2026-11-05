# ADR 2026-11-05: Dokumentera avslutade EVCC-laddsessioner och precisera inhibit-orsaker

**Status: Driftsatt och verifierad på Dell 2026-11-05.** Ingen
ändring av EVCC:s fysiska styrning, dispatcher eller Modbus-broker. Kompletterar
[EVCC:s tidigare paus-/återstartsskydd](adr-2026-11-04-ev-price-empirical-live.md)
och [webbens laddlägen](adr-2026-11-05-ev-web-live-mode-selection.md).

## Observerat problem

Volvo kunde visa 77 % när laddkabeln drogs ut på morgonen 2026-11-05.
Efter en bilresa visade dagens status 74 %. Webbplatsen visade enbart
det senaste värdet, utan att kunna visa nivån **när laddningen avslutades**.
Dessutom svarade priscontrollern `no calibrated fresh EV session` både
när den låsta avresetiden 07:00 passerade och när kabeln senare drogs ut.

Bekräftad historisk tidslinje i svensk tid:
- 07:09:08 EVCC `C2/PWM100`, cirka 6,1 A.
- 07:09:38 EVCC `B2`, strömmen upphörde.
- 07:10:08 första `A1`-observationen.
- 07:10:41 andra `A1`-observationen och beständig
  `last_disconnected_at_utc` i kabelspåraren.
- Volvos senaste användbara före-frånkopplings-API visar **77 %**.
  En senare observation efter bilens avfärd visar **74 %**.

En EVCC FC03-observation sker var cirka 30:e sekund. Ovanstående
tidpunkter är därför **observerade**, inte garanterade millisekund-
eller fysisk urkopplingstid.

## Beslut: beständig, minimal händelse ur befintlig historik

Ny läsande modul `scripts/services/ev_charge_end.py`, körd inuti
**befintliga** `solinteg-ev-history.service` utan ny timer eller nätverk:
- Endast en dubbelprovsbekräftad kabelspårarövergång till
  `disconnected/A1|A2` med samma `last_transition_at_utc`
  och `last_disconnected_at_utc` är en accepterad sessiongräns.
  Filen måste ha färskt FC03-prov, giltigt spårarschema och
  beständigt ägarskap.
- Minst en föregående, verifierad `C2`-laddobservation med
  **Ict2 5,5–6,5 A** inom 24 timmar före urkopplingen krävs.
  En kort anslutning utan faktisk laddning ger ingen falsk session.
- SoC hämtas enbart från **samma, privata 30-sekundershistorik**,
  med färsk/brukbar Volvo API-observation högst fem minuter före
  urkopplingen. Eventets SoC är **fryst** och uppdateras aldrig
  från senare körning, även om bilens aktuella SoC sjunker.
  Saknas sådant underlag skrivs **NULL**, aldrig 0 eller en
  bakåtfylld BMS-siffra.
- Beständiga händelser lagras i den nya, småskaliga SQLite-
  tabellen `ev_charge_ends` i **befintliga privata**
  `ev-history.sqlite`. Primärnyckeln är bekräftad
  urkopplingstid i UTC, `INSERT OR IGNORE` ger idempotens.
  Råsampletabellens 30-dagarsretention raderar inte
  verifierade sluthändelser.
- Historikern publicerar enbart **senaste** avslutade session
  som atomisk, grupp-läsbar **0640** JSON
  `/opt/solinteg/ev-shadow-runtime/var/ev-last-charge-session.json`.
  SQLite förblir **0600** och inte läsbar från webben.
  Exportens `quality=polled_cable_and_api_reported_soc_not_bms`
  förhindrar att den framstår som en fysisk batterimätning.
- `/solinteg/ev` visar en separat post
  **Senaste avslutade laddsession · Frånkopplad [tid] · [SoC] %**
  och uttrycklig reservation för pollad kabeltid/Volvo API-SoC.
  Det aktuella Volvo-värdet visas oförändrat och separat.
- Saknat/trasigt underlag får aldrig trigga EVCC-kommandon.
  Historikexportfel loggas men stoppar inte 30-sekunderssamplern.

## Priscontrollerns separata, läsande statusklassificering

Vid `session is None` bibehålls **exakt samma fysiska spärrar,
ägarrevoke och `writes=0`**. Endast förklaringen får ändras:
1. Färsk, verifierat frånkopplad `A1/A2`:
   **Bilen är frånkopplad; inga laddkommandon skickas**.
2. Annars: beständigt `deadline`-ledger med oförändrad
   inställningsrevision och förfluten absolut UTC-avresetid:
   **Avresetiden har passerat; den tidigare laddsessionen har löpt ut**.
3. Annars kvarstår den diagnostiskt korrekta, generella
   `no calibrated fresh EV session`.

Det finns **ingen** ny start-/pausregel, mer generös
sessionsbehörighet, annan elprisplanering eller ytterligare
FC10-kod. Webbens `inhibited`-etikett presenteras som
**Styrning spärrad**, inte som driftfel.

## Kända begränsningar

- 30-sekunderspollningen kan missa mycket korta ur-/återinkopplingar.
- Volvo API:s `last_changed` är inte ett BMS-provtillfälle
  och 77 % är därför användbart **rapporterat** SoC, inte
  en oberoende fysisk mätning.
- Om kabelspårning, FC03, verifierad tidigare laddström eller
  färskt API-SoC saknas redovisas ingen händelse respektive
  okänd SoC. Avslut vid en tidigare laddpaus utan bevisad
  laddström inom 24 h arkiveras inte som avslutad laddsession.
- En deadline kan gå ut medan bilen faktiskt laddar; texten
  klassificerar den **gamla missionens** ogiltighet, den
  utfärdar inte ett kommando för att stoppa bilen.
- Den nya tabellen är långlivad och bör omfattas av krypterad
  arkivering av privat historian. Inga privata tabellrader
  eller fordonsidentifierare checkas in i Git.

## Kod, tester och faktisk drift

- Fullständiga Python-, Vitest-, TypeScript- och ESLint-kontroller
  passerade inför driftsättningen. Nya riktade tester täcker
  bekräftad urkoppling/77 %, senare 74 %, idempotens,
  ofullständig SoC, uteblivet laddbevis och retention.
- Offline-fältprov mot en **privat SQLite-säkerhetskopia** av
  verkliga driftdata återskapade 2026-11-05
  **07:10:41 CET / Volvo 77 %** utan fysisk skrivning.
- Uppdaterade endast befintliga
  `solinteg-ev-history.service` (ny modul +
  importerad samplingskrok), befintliga
  `ev_price_controller.py` och tre Next.js-källfiler;
  Next byggdes i separat katalog baserad på redan
  installerade webbversionen. Den gamla Next-bunten
  flyttades till en skyddad rollback.
- Den aktiva privata historikdatabasen fick en ny tabell
  med **en** bekräftad händelse vid
  `2026-11-05T06:10:41.173246+00:00`, `soc_pct=77`.
  Gruppexporten `ev-last-charge-session.json`
  är `0640`; ursprungs-SQLite förblir `0600`.
- `GET /solinteg/ev` gav HTTP 200. `GET
  /solinteg/api/ev` visade separat
  `lastEndedSession.socPct=77` och aktuell
  `status.battery_soc_pct=74`, tillsammans med
  färsk controllerstatus
  **Bilen är frånkopplad; inga laddkommandon skickas**.
  Tidigare avresetidsdiagnostik är simulerat
  testad, inte retroaktivt inskriven i journal.
- Observer, historian, priscontrollertimer och webb var
  aktiva vid slutverifiering. Ingen fysisk FC10 utfördes.
  Den befintliga systemd-servicens arbetsvillkor, fasta
  6 A-gräns och ägarfiler har inte ändrats.

**Rollback och artefakthantering:** Privat, rootägd
`0700`-katalog
`/var/lib/solinteg-evshadow-rollback/ev-end-20261105/`
innehåller byteexakta ursprungliga fem filer,
en säker SQLite-snapshot, fältprovets sanerade
JSON, manifest och förra Next.js-bygget.
Vid rollback kontrolleras först verkligt EVCC-tillstånd
och att inget pausägarskap är oavslutat. Återställ
endast de tidigare kopierade modulerna/källfilerna
och Next-bygget, starta om **endast**
`solinteg-ev-history` och `solinteg-web`.
Priscontrollern är oneshot och laddar kod vid nästa
minutcykel; den befintliga timern ska normalt vara kvar.
Den nya händelsetabellen behöver inte raderas
för kodrollback, och historiska uppgifter eller
pågående pausägarskap får inte förloras.
Privata rollbackkopior behålls tills krypterad
offsitearkivering och återställningskontroll
är verifierade.
