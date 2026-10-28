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
- `scripts/services/evcc_service.py`: `--watch` sparar endast skuggbeslut
  i separat `evcc-shadow.json`; ingen FC10 på den vägen. `EV_ENABLED=0`
  (default) betyder **ingen EVCC-anslutning**, inga skrivningar eller
  effekter på `optimizer_runs` och befintlig batteridispatch.
- `deploy/solinteg-evcc.service`: **inaktiv** systemd-unit; ingen automatisk
  installation, start, deploy eller ändring av skarp Solinteg.

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
