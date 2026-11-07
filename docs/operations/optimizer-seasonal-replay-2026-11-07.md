# O04 · Säsongs-, DST- och prognosbortfallsreplay: kvarvarande NO-GO

**2026-11-07 – `tested/source`, ingen publik plan eller produktionsstyrning ändrad.** `lib/__tests__/optimizer-seasonal-replay.test.ts` testar optimerarens **rena** `optimizeDispatch` på syntetiska 15-minutersprofiler. Scenarierna är in-memory med deterministiska spot-/PV-/huslast-/EV-kurvor, inte faktiska Sundsvallsmätningar eller fysiska Modbus/EVCC-prover.

| Scenario | Antal 15-minutersslotar | Utfall |
| --- | ---: | --- |
| Normal vinter / normal sommar | 96 / 96 | 2/2 deterministiska, SoC-, effekt-/grid-gränser inom källans modell |
| Förkortad vårdag / förlängd höstdag (DST) | 92 / 100 | 2/2 tekniskt giltiga slotmängder; endast test av tidsaxelns 15-minuterskontinuitet i UTC, inte oberoende bevis för varje Stockholm-datums local-midnight-ankare |
| Kort prognoshorisont | 8 (= 2 timmar) | Kör igenom utan källfel men **osäkert ekonomiskt resultat**, se nedan |
| En saknad solprognosslot | 96, med ett saknat PV-värde ersatt med 0 i fixture | Kör igenom, men kvaliteten måste markeras som härledd/nedsatt vid publicering |
| Stigande/fallande och negativa priser | 96 per scenario | Deterministiska och begränsade effekt-/SoC-värden |
| EV-laddlast under solproduktion | 96 | Optimerad total last inom modellens grid cap, **inte** bevis på fysisk batteri→bil-isolering |

**10/10 fokuserade Vitest-fall** passerade inklusive ett explicit **karakteriseringstest för känd risk**. Den sista är en medveten regressionsdetektor, inte ett acceptanskriterium för säkerhet.

## Kritisk reproducerad kort-horisontseffekt

Två syntetiska planeringar med **identiskt initialt SoC och samma första åtta 15-minuterspriser/last/PV** men total horisont 96 respektive 8 slotar gav vid tvåtimmarsgränsen:

- `short.endSoc - full.out[7].socAfter = -15,2107 kWh`.
- Den korta prognosen väljer alltså **15,21 kWh lägre** ackumulerat SoC än den längre prognosen över **samma första två timmar**.
- Det här värdet är för den syntetiska modellen och dess då gällande terminalvärde, inte en uppmätt effekt i installerad inverter eller en garanti att bilens 80 %-mål påverkas.
- Planens nuvarande `lib/plan.ts` **loggar** kort horisont inklusive positiv terminalenergi, men stoppar inte nödvändigtvis dess publicering. Ett grönt test av källans grid-/SoC-bounds utesluter **inte** denna ekonomiskt oönskade batteritömning.

**O04 förblir öppen och ger NO-GO för automatisk planering vid kollapsad prognos utan ytterligare explicit skydd.** Nästa säkra steg är en separat, testad, default-off source-grind för en minimihorisont eller ett terminalt SoC-värde, med replay mot redan publicerade verkliga planer och ett scenario för legitim 23h-DST-dag. Varje live-ändring av publicerad plan kräver separat operatörsgodkännande och jämförelse mot installerad `lib/plan.ts` (`R04`). Under tiden ska alarm/kvalitet flagga `short_horizon`; ändra inte SoC-skydd och försök inte forcera fysisk General.

**Separata begränsningar:** `optimizerSlots` i en riktig publicering trimmas med `trimIncompleteTerminalDays`; en förlorad prognos eller 25h-dag kan slå annorlunda än dessa rena DP-fixtures. Planens 80%-EVCC-session och faktisk spot-/nätkostnad är inte modellerade i detta replay. Full acceptans för O04 kräver därför modell-, planerings- och driftkvalitet, inte bara de tio gröna testfallen.
