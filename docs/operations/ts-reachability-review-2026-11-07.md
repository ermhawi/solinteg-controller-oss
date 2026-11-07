# A05 · TypeScript nåbarhet, featureflaggor och modellkollisioner

**2026-11-07 – `tested/source`, 0 borttagningar.** Reproducerbar käll- och AST-granskning med TypeScript compiler API, `scripts/tools/ts_reachability_audit.mjs`. Maskinläsbar rapport: [ts-reachability-2026-11-07.json](ts-reachability-2026-11-07.json), samt **5/5** Node-built-in-kontroller i `scripts/tools/verify_ts_reachability.mjs`. Granskningen exekverar aldrig Next-kod eller maskinvara, läser inte installerad `/opt` eller privata miljövärden, och föreslår ingen radering.

| Kontroll | Faktisk undersökning och bevis |
| --- | --- |
| Produktion TS/TSX | **87/87 källmoduler nåbara** från **18** Next-entrypoints (`app/**/{page,layout,route,...}`); den äldre baslinjen var 83/83 före de nya källfilerna i T02 |
| Döda filer/grenar | **0** ej nåbara produktionsmoduler, **0** direkt literal `if(false)`/`if(true)`/statisk ternär med konstant villkor och **0** uppenbara beräknade dynamiska importer i denna begränsade AST-granskning |
| Felaktiga lokala importvägar | **0** olösta relativa/`@/` TS- eller TSX-modulimporter (CSS, JSON och virtuella Next-filer är undantagna på avsiktligt sätt) |
| Featureflaggor | **28 olika `process.env.NAME`-nycklar** och respektive kodposition dokumenterade. **Inga** effektiva envvärden är inlästa, och flaggnamnets existens eller källdefault är inte bevis för inaktiverad kod |
| Modellnamnskollision | **`EvChargeMode` deklareras två gånger**: exporterad union `'deadline' | 'economy'` i `lib/ev-settings.ts`, lokal samma union i `lib/ev-soc-state.ts` med avsiktlig kommentar om separat /ev-kedja. Semantiken är identisk men arkitekturgränsen kan vara medveten. **BEHÅLL**, inte en säker auto-deduplicering |
| Raderingsförslag | **0**. Inga types, filer, featuregrenar eller äldre modeller har tagits bort |

**Riskkvalificering:** Granskningen finner inte komplexa döda grenar om villkoret är bundet till runtime-data, en .env-flagga, ett booleskt wrapperuttryck eller en transformerad expression. Att 87/87 nås i importgrafen **bevisar inte** att varje funktion, branch eller export faktiskt används. `process.env.SOLINTEG_HOLD_ENABLED`, `SOLINTEG_REFUND_CREDIT`, EVCC-write-flaggor och OAuth2-begränsningar måste kvalificeras mot effektiv drift för eventuella beteendeförändringar; att radera dem skulle inte vara en statisk kvalitetsfix.

**Definition of Done här:** Verifierad täckning för samtliga 87 produktionsmoduler, explicita granskade kollisions-/flagg-/branchfynd med proveniens och **inga ogrundade raderingsförslag**. Framtida A06 (borttagning) och A07 (ny inventering efter en faktisk borttagning) står öppna och kräver särskilda reachability-/test-/rollbackbevis. `node scripts/tools/verify_ts_reachability.mjs` körs i den read-only GitHub Actions-webbgrinden utan deploy och utan fysiska tester.

```bash
node scripts/tools/verify_ts_reachability.mjs
node scripts/tools/ts_reachability_audit.mjs
# Valfritt: --output /repo/ny-sanerad-rapport.json, vägrar skriva över
```
