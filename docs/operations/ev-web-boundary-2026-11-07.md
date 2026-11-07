# E05 · EV-webbens skrivgräns, API-body och OAuth-verifiering

**2026-11-07 — `tested/source` och läsande installerad säkerhetskontroll, inte komplett E05.** Ingen produktionskod, auth-konfiguration, laddinställning, inverter, EVCC eller systemd har ändrats.

## Reproducerade brister och åtgärdade källkontrakt

| Negativt test före korrigering | Tidigare beteende | Korrigerad källväg |
| --- | --- | --- |
| Giltig slutlig JSON-mission efter en stor duplicerad UTF-8-nyckel, JS-sträng under 2048 tecken men över 2048 byte | **200** och sparad settingsfil istället för **413** | `app/api/ev/settings/route.ts` läser högst **2048 verkliga byte** via Reader + TextDecoder, nekar **413** före JSON-parse och skriver ingenting |
| `Origin: http://...` bakom `X-Forwarded-Proto: https`, eller multipel/motsägande proxy-proto | **200** trots schemamismatch | Kräver exakt `http` eller `https`, samma schema som `Origin`, och samma `host`; avvisar **403** |
| `Content-Type: application/jsonp`, negativ/fraktionell `Content-Length` | Accepterades / hamnade i senare felväg | Exakt JSON-medietyp (tillåter `application/json; charset=utf-8`), heltalsbytegräns och minimum noll; fel **415/413** |

Testerna i `lib/__tests__/ev-settings-route.test.ts` använder enbart isolerad temporär settingsfil under `/tmp` och kallar den rena route-funktionen lokalt. Källkodens returvärde `deviceWrites=0` och `app/components/EvChargingPlanner.tsx` verifieras: endast `fetch(apiPath('/api/ev/settings'), method=PUT, credentials=same-origin)`, inga direkta `EvccAscii`, FC10 eller Modbus-skrivningar. Ett streamingtest med **felaktigt deklarerade 2 byte** och en faktisk body över 2048 byte kräver **413** och ingen sparad settingsfil. **9/9 riktade route-tester** har passerat efter fix; tre tidigare av dem var avsiktligt röda innan minsta korrigering. Full `npm test`: **789/789 körda godkända, 1 skipped**; `npm run typecheck`, strikt `tsc --noUnusedLocals --noUnusedParameters` och `npm run lint` PASS. `npm run build` på Dell är **inte** grönt: lokal Turbopack/Google-font-import eller felvald monorepo-root gav 18 fontresolver-fel; separat `next build --webpack` kompilerade men visade den redan existerande inkompatibla route-exporten `addDays` i `app/api/oracle/route.ts`. Detta är **inte** testbevis för säker release. Ingen Next-/OAuth2-installation uppdaterades.

**Begränsning:** `X-Forwarded-Host` och `X-Forwarded-Proto` är bara meningsfulla när Traefik rensar eller sätter dem. Den interna Next-route-funktionen autentiserar inte användaren; den förlitar sig avsiktligt på separat nätgräns och OAuth2. Ändrade klienthuvuden får därför **aldrig** tolkas som GitHub-identitet. Ett externt API via SNAT/alternativ ingress kan påverka tillitsmodellen och behöver separat kontroll.

## Läsande installerad observation

- `solinteg-web.service`, `solinteg-web-internal-only.service`, `solinteg-ev-price-controller.timer`: **loaded/active** vid kontrollen.
- `kubectl -n solinteg get ingressroute solinteg -o json`: **två rutter**, `/_next` och `/solinteg`, båda refererar `oauth2`-middleware. Ingen middlewarepolicy eller token ändrades.
- `sudo nft -j list table inet solinteg_web_internal`: en etablerad tabell med **två drop-regler** (match/counter/drop), läst utan mutation; den faktiska routerns alla möjliga omvägar/SNAT är **inte** oberoende granskade.
- Anonymt read-only GET via `127.0.0.1:3007/solinteg/api/ev` gav **200** (förväntad intern tillitsgräns). Anonymt read-only GET via `https://ermhawi.com/solinteg/api/ev` gav **401** (förväntad yttre OAuthgrind). Inga cookies eller inloggningsuppgifter samlades in och **inget skarpt PUT** gjordes.

## E05:s återstående NO-GO

**E05 är öppet** även med en grön source-/CI-svit. Acceptanskriteriet kräver ett **autentiserat externt E2E- och nekande skrivtest** med verifierad GitHub-identitet, avvisad obehörig session, bindning till **exakt** verksam settingsrevision och kontrollerat oförändrade fysiska FC10/automatiska starter. Det innebär risk för sparad laddavsikt och måste planeras som separat operatörsgodkänt, reversibelt test. En läsande 401 eller kodtest kan inte ersätta detta.

**Nästa steg:** separat testfönster med explicit GO, före-/efterhash av privata inställningar och sessions-/owner-revision, inga oväntade automatiska laddstarter, samt verifierad rollback. Ingen ändring i aktiv OAuth-proxy eller systemd föreslås.
