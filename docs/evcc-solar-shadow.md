# EVCC: två laddlägen, pris och solöverskott (skuggläge, 2026-10-28)

**Status: kod och offlinetester klara, INTE driftsatt eller hårdvaruverifierad.**
Följ etappgränserna i `docs/superpowers/specs/2026-10-19-smart-ev-charging-design.md`.

## Kod

- `scripts/services/evcc_ascii.py`: en enhet, Modbus ASCII över transparent TCP,
  FC03 `0x000F` och `0x002E`, LRC, kontroll av svar och begränsat läs-retry.
  En enda FC10 till `0x0014` endast efter separata manuella spärrar, exakt
  förväntat råbörvärde och efterföljande läsback. Paus är `0x03E8` (100 %
  PWM, ingen laddström), **inte** 0 A. Start/återupptagning kräver en extra
  manuell startsignal. Ingen automatisk upprepning efter tvetydigt ACK.
- `scripts/services/ev_solar_policy.py`: rent skuggbeslut med separata
  bidrag från solel och pris/tidsplanerad nätel. Solöverskott får användas
  först vid minst 99,5 % SoC i husbatteriet (99 % vid pågående laddning)
  och tre stabila observationer. Planerad **billig nätel eller nödvändig
  deadlinesladdning** kan däremot föreslås även innan huset nått 100 %.
  Husbatteriets urladdning >150 W, gamla mätvärden och saknat eller
  uppnått bilbatterimål stoppar ändå varje bil-laddningsförslag.
  Överskottet räknas med `grid_w + EVCC-uppmätt EV-effekt - 200 W`
  så att bilen inte skapar sin egen start/stopp-pendling.
  Det befintliga 150 W-skyddet är fortfarande för konservativt när
  batteriet legitimt försörjer husets grundlast: ett EV-medvetet
  batteriskydd måste ersätta det före automatisk drift.
- `scripts/services/ev_price_policy.py`: två laddlägen som båda läser
  husbatteriets redan publicerade `optimizer_runs.inputs_json` (`buyPrice`
  med skatt, moms, nätavgift och påslag, samt `sellPrice` för export).
  **`deadline`:** prioritera 80 % vid avresa, fördela energibehovet på
  billigaste möjliga kvartarna; om tidsmarginalen tar slut rekommenderas
  laddning oavsett köppris, även vid prisbortfall. **`economy`:** ingen
  avresetid, använd bara relativt billiga elpriser – lägsta 25 % av upp
  till 48 h framtida köppriser och strikt under medianen när medianen är
  positiv. Valfri **absolut prisgräns** `max_buy_ore_per_kwh` ger användaren
  kontroll vid genomgående höga elpriser. Värdera sol med utebliven
  exportintäkt; ladda aldrig med solel som husets plan avsatt till
  husbatteriet. Båda stoppar laddningsförslag vid bilens 80 %-mål.
  Prisprognosernas riskpåslag återanvänder
  `SOLINTEG_FORECAST_RISK_PREMIUM_ORE`. Vid okända eller tvetydiga
  priser avstår `economy` från nya laddstarter.
- `scripts/services/evcc_service.py`: `--watch` sparar endast skuggbeslut
  i separat `evcc-shadow.json`, inklusive `charge_mode`,
  `target_soc_pct`, `departure_at_utc` (null i `economy`),
  `remaining_ev_ac_kwh`, `price_buy_ore_kwh`, `price_sell_ore_kwh`,
  `price_cheap_threshold_ore_kwh`, `grid_topup_suggested_a` och `price_reason`.
  Ingen FC10 på den vägen. `EV_ENABLED=0` (default) betyder **ingen
  EVCC-anslutning**, inga skrivningar och inga effekter på `optimizer_runs`
  eller befintlig batteridispatch.
- `deploy/solinteg-evcc.service`: **inaktiv** systemd-unit; ingen automatisk
  installation, start, deploy eller ändring av skarp Solinteg.

## Delat elpris, men ännu inte gemensam verkställande plan

Prisbesluten kommer från samma optimerarkörning som husbatteriet:
`buyPrice = (spot + leverantörspåslag) × (1 + moms) + skatt/överföring` och
`sellPrice = faktiskt exportvärde`. Till skillnad från husbatteriet kan
Volvon inte sälja tillbaka den köpta elen. De två laddlägena uttrycker
**olika mål** med **samma ekonomi och säkerhet**:

1. **`deadline` / Klar till avresa:** 80 % vid fast UTC-avresetid är
   förstahandsmålet. Välj billigaste möjliga tidsfönster men använd även
   dyr el när det annars inte går att hinna. För kort laddfönster eller
   otillräcklig fasbudget ska visas som ett olösbart mål, inte påstås vara
   en garanti. Om prisinformationen saknas kan enbart tidskritisk laddning
   föreslås; felaktiga/tvetydiga snapshots används aldrig.
2. **`economy` / Ladda ekonomiskt:** **ingen avresetid** och inget
   åtagande om när bilen blir klar. Ladda när dagens uppmätta importpris
   är inom relativt billigaste kvartilen av tillgänglig 48 h-prognos,
   eller när det faktiska exportvärdet av solelen är lika lågt. En
   valfri, explicit totalpristaksgräns i öre/kWh kan vara striktare.
   Relativt billig är **inte** alltid billigt i absoluta kronor; ett
   användarvalt tak behövs om det är viktigt. Om prisunderlaget inte
   räcker för minst 6 h jämförelse avstår läget från laddstart.
   Målet är fortfarande 80 %, därefter stannar alla förslag.

**Begränsad första leverans:** laddplanen är en marginalkostnadsordning
med *husets publicerade plan fixerad*. Den är **inte** ännu en gemensam
tvålagersoptimering som omplanerar hus- och bilbatterierna ihop. Framtida
samordnad planering måste ta hänsyn till att bilens last förändrar husets
batteriplan och dess intäkter; den får inte aktiveras bara för att denna
skuggberäkning klarar offlinetester.

Före laddningsförslag krävs en **separat producerad** färsk
`EV_SESSION_PATH`-JSON från en betrodd read-only HA-läsare:

```json
{
  "source": "home_assistant",
  "connected": true,
  "observed_at_utc": "2026-10-28T10:00:00Z",
  "charge_mode": "economy",
  "target_soc_pct": 80,
  "vehicle_soc_pct": 67,
  "remaining_ac_kwh": 10.2,
  "max_buy_ore_per_kwh": 120
}
```

Exemplet är **schemaillustration, inte en mätning eller rekommenderat
pristak**. I `deadline` byts `charge_mode` och man lägger till en
fast `departure_at_utc` i UTC. I `economy` finns **ingen avresetid**,
och `max_buy_ore_per_kwh` är **valfritt**. Oavsett läge ska
`remaining_ac_kwh` härledas från verifierad användbar batterikapacitet,
laddförluster och bilens *käll-tidsstämplade* SoC; sätt aldrig
`observed_at_utc` till nu om källvärdet är gammalt.

**Producenten och UI-valet är inte byggda ännu.** Utan en färsk,
validerad fil blir både nätandelen och det slutliga laddförslaget 0 A.
Det här är endast ett stopp i **planeringens förslag** vid 80 %, inte
en fysisk stoppfunktion. För skarp drift måste bilens lokala
80 %-gräns och EVCC:s paus/readback verifieras oberoende; HA:s
molnfördröjning räcker inte som säker sista stoppåtgärd.
`EV_SESSION_PATH` ska inte fyllas med gissad SoC eller nollställas vid
varje poll. Unika UTC-tider behövs vid sommartidsbyte; gamla
tvetydiga 02:xx-kvartar ger inget nätladdningsbeslut.

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
