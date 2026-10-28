# EVCC-solöverskott: isolerad implementation (2026-10-28)

**Status: kod och offlinetester klara, INTE driftsatt eller hårdvaruverifierad.**
Följ etappgränserna i `docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md`.

## Kod

- `scripts/services/evcc_ascii.py`: en enhet, Modbus ASCII över transparent TCP,
  FC03 `0x000F` och `0x002E`, LRC, kontroll av svar och begränsat läs-retry.
  En enda FC10 till `0x0014` endast efter separata manuella spärrar, exakt
  förväntat råbörvärde och efterföljande läsback. Paus är `0x03E8` (100 %
  PWM, ingen laddström), **inte** 0 A. Start/återupptagning kräver en extra
  manuell startsignal. Ingen automatisk upprepning efter tvetydigt ACK.
- `scripts/services/ev_solar_policy.py`: rent skuggbeslut. Start vid minst
  99,5 % SoC, tre stabila observationer och tillräckligt nettoöverskott
  för minst 6 A + 200 W marginal. Tillåter fortsatt drift ned till 99 %;
  föreslår stopp vid för liten solel, urladdning >150 W eller gamla data.
  Överskottet beräknas med `grid_w + EVCC-uppmätt EV-effekt` för att inte
  skapa en reglerloop där bilens egen effekt trollar bort exporten.
- `scripts/services/ev_price_policy.py`: prisplanering i skuggläge utifrån
  husbatteriets redan publicerade `optimizer_runs.inputs_json` (`buyPrice`
  inklusive skatt, moms, nätavgift och påslag samt `sellPrice` för export).
  Denna kod uppfinner **ingen separat gräns för billig el**. Den värderar
  tillgänglig framtida solel med utebliven exportintäkt och nätköp med samma
  fullständiga köppris som husets optimerare. Billigaste genomförbara
  intervall väljs för det kända återstående AC-energibehovet före avresan.
  Projektets redan publicerade `solarExportKwh` skyddar solel som husets
  batteriplan avsett att lagra. Prognospriser hanteras med husets befintliga
  `SOLINTEG_FORECAST_RISK_PREMIUM_ORE`.
- `scripts/services/ev_solar_policy.py`: föreslår solöverskott plus endast
  den nätandel som valts av prisplaneringen. Husbatteriet prioriteras vid
  minst 99,5 % SoC inför start (99 % under laddning), och batteriurladdning
  >150 W blockerar fortsatt EV-laddning. Solöverskott kontrolleras fortfarande
  fysiskt med `grid_w + uppmätt EV-effekt - 200 W`; historiska prognoser
  får inte påstå att nätimport är solel. Ingen ampere under 6 A skickas som
  förslag; en eventuell delkvart kräver ett särskilt tidsbegränsat stopp.
- `scripts/services/evcc_service.py`: `--watch` sparar endast skuggbeslut
  i separat `evcc-shadow.json`, inklusive `price_buy_ore_kwh`,
  `price_sell_ore_kwh`, `grid_topup_suggested_a` och `price_reason`.
  Ingen FC10 på den vägen. `EV_ENABLED=0` (default) betyder **ingen
  EVCC-anslutning**, inga skrivningar och inga effekter på `optimizer_runs`
  eller befintlig batteridispatch.
- `deploy/solinteg-evcc.service`: **inaktiv** systemd-unit; ingen automatisk
  installation, start, deploy eller ändring av skarp Solinteg.

## Delat elpris, men ännu inte gemensam verkställande plan

Prisbesluten kommer från samma optimerarkörning som husbatteriet:
`buyPrice = (spot + leverantörspåslag) × (1 + moms) + skatt/överföring` och
`sellPrice = faktiskt exportvärde`. Till skillnad från husbatteriet kan
Volvon inte sälja tillbaka den köpta elen; den måste nå sitt energimål
senast avresa. Nätet kan därför komplettera solelen när de billigaste
genomförbara kvartarna före avresa valts. Vid dyra kvartar används
enbart uppmätt solöverskott. Om dagens kvart är dyr men flera framtida
kvartar är billigare väntar bilen, så länge tillräcklig laddtid återstår.

**Begränsad första leverans:** laddplanen är en marginalkostnadsordning
med *husets publicerade plan fixerad*. Den är **inte** ännu en gemensam
tvålagersoptimering som omplanerar hus- och bilbatterierna ihop. Framtida
samordnad planering måste ta hänsyn till att bilens last förändrar husets
batteriplan och dess intäkter; den får inte aktiveras bara för att denna
skuggberäkning klarar offlinetester.

Före nätbaserade prisförslag krävs en **separat producerad** färsk
`EV_SESSION_PATH`-JSON från en betrodd read-only HA-läsare med:
`source="home_assistant"`, `connected=true`, `observed_at_utc`,
`departure_at_utc` och `remaining_ac_kwh` (beräknat från verifierad
kapacitet, laddförluster och bilens *käll-tidsstämplade* SoC).
Producenten är **inte byggd ännu**; utan filen blir nätandelen 0 A
och skuggbeslutet använder bara solöverskott. `EV_SESSION_PATH` ska
inte fyllas med manuellt gissad SoC eller återställas vid varje poll.
Unika UTC-tider behövs vid sommartidsbyte; gamla tvetydiga 02:xx-kvartar
ger inget nätladdningsbeslut.

`EVCC_MAX_CURRENT_A` är högst 6 som standard tills fas- och EVCC-skydd
verifierats. `solar_permitted_a` förblir 0 eftersom oberoende L3-skydd
inte är verifierat. Befintliga säkerhetsspärrar, batterirecovery och
stopp vid för gammal data har företräde framför båda energikällorna.

## Statuskontroll utan skrivning

Från utvecklingsutcheckningen, med `EV_ENABLED=0`:

```bash
python3 scripts/services/evcc_service.py
python3 -m unittest discover -s scripts/tests -p 'test_ev*.py' -q
```

Om Waveshare är åtkomlig och **ensam EVCC-länkägare** kan en read-only
statushämtning göras med enbart `EV_ENABLED=1` och `EVCC_HOST` satt till
den verifierade gatewayen (port 4196). `EVCC_CONTROL_ARMED` lämnas 0.

## Fysiskt prov: inte godkänt av kodtest

En eventuell manuell paus/återupptagning kräver att en installatör eller
operatör först verifierar 20 A-fasens RMS-ström, bilens 6 A-tak, aktuell
Modbus-status, och har en oberoende väg att stoppa laddningen. Före ett
separat tidsbegränsat prov måste även aktuellt EVCC-råbörvärde sparas.

Manuella CLI-kommandon kräver **alla** följande:
`EV_ENABLED=1`, `EVCC_CONTROL_ARMED=1`, `EVCC_MANUAL_TEST=1`,
`--confirm`, `--expected-raw`. Återupptagning kräver dessutom
`EVCC_START_APPROVED=1`. Dessa värden får inte sättas i normal
systemd-drift. En bekräftad FC10-kvittens är inte en verifierad fysisk
paus. Vid utebliven kvittens behandlas status som **okänd**; ingen
automatisk retry eller återstart görs.

**Hårdvarublockering 2026-10-28:** Dell rapporterade `No route to host`
mot den tidigare verifierade Waveshare-adressen/porten; ingen FC03 eller
FC10 kunde testas live. EVCC:s upstream-timeout har tidigare visats
vara avstängd. Utan ett verifierat *oberoende* stoppskydd/fasbudget får
ingen automatisk EVCC-styrning aktiveras; `solar_permitted_a` är alltid
0 i den aktuella skuggservicen. `solar_suggested_a` är **inte** en säker
styrsignal.

Vid återupptag: verifiera länken, status, paus `0x03E8`, laddström 0 A,
återstart 6 A, avbruten kommunikation och fysisk säkerhetsfunktion.
Dokumentera resultat före beslut om begränsad pilot.
