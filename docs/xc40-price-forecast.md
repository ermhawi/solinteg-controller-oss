# XC40 – daglig SE2-elprisprognos (beslutsstöd, ingen styrning)

En daglig ChatGPT-automation kl. 14:15 Europe/Stockholm kan publicera
prisutsikt för 3–7 dagar. Publishern är fristående från Solintegs dispatch,
DEFA/EVCC, Volvo och Home Assistants interna databas.

## Flöde

1. ChatGPT hämtar verifierade spotpriser för SE2, väder- och produktionsdata
   och gör ett **osäkert** prognosscenario; ange källor i rapporten.
2. ChatGPT skickar ett strikt JSON-dokument via stdin till
   `scripts/services/ev_price_forecast_publish.py` med mcpsh47.
3. Publishern validerar fälten, kalenderdatum, rimliga tal, kvalitet och
   publika källadresser. Okända fält, hemligheter, för gamla körningar,
   bakåtdaterade/icke sammanhängande dagar, NaN, okända källor och felaktiga
   rekommendationer avvisas utan att ändra tidigare publicerad JSON.
4. Varje giltig körning sparas i privat
   `/opt/solinteg/ev-shadow-runtime/var/xc40-price-forecasts.sqlite`
   och publiceras atomiskt som
   `/opt/solinteg/ev-shadow-runtime/public/xc40-price-forecast.json`.
5. Home Assistant läser endast den publika JSON-filen med befintligt
   read-only hostPath `/xc40-cost-rate`. Sensorn blir `unavailable`
   när publiceringen är äldre än 36 timmar. Lovelace-grafen uppdateras
   utan commit.

### Inmatningskontrakt

Skicka **endast** JSON i följande format (alla tal här är **syntetiska**,
publicera aldrig exemplet som en verklig prognos):

```json
{
  "schema": "xc40_se2_price_forecast_input_v1",
  "area": "SE2",
  "local_date": "2026-10-29",
  "recommendation": "wait",
  "reason": "Helgen kan bli billigare än fredagen, men prognosen är osäker.",
  "best_charge_date": "2026-10-31",
  "savings_kr_20kwh": 3.5,
  "savings_kr_40kwh": 7,
  "days": [
    {"date":"2026-10-29","type":"published","price_ore_kwh":30,"low_ore_kwh":30,"high_ore_kwh":30},
    {"date":"2026-10-30","type":"published","price_ore_kwh":50,"low_ore_kwh":50,"high_ore_kwh":50},
    {"date":"2026-10-31","type":"forecast","price_ore_kwh":20,"low_ore_kwh":12,"high_ore_kwh":40},
    {"date":"2026-11-01","type":"forecast","price_ore_kwh":25,"low_ore_kwh":15,"high_ore_kwh":45}
  ],
  "sources": [
    {"name":"Nord Pool","url":"https://www.nordpoolgroup.com/"},
    {"name":"Open-Meteo","url":"https://open-meteo.com/en/docs"}
  ]
}
```

- `local_date` är dagens datum i Europe/Stockholm vid publiceringen.
- `days` måste börja idag, vara datumordnade utan luckor och ha 3–7
  dagar. Publicerat pris får endast avse idag eller imorgon. En verklig
  prognos för framtida dagar anges som `forecast`; aldrig kalla dessa
  spotpriser fastställda. Kända priser har low=price=high.
- `price_ore_kwh` är dygnsmedel för spotpris **utan** moms, skatter och
  nätavgifter. `low_ore_kwh` och `high_ore_kwh` är scenariointervall,
  **inte** kalibrerade P10/P90 om inget stöd finns. Negativa priser är tillåtna.
- `recommendation`: `charge_today`, `wait` eller
  `no_clear_difference`. `best_charge_date` är dagen som rekommenderas,
  eller null vid ingen tydlig skillnad. Vid `wait` måste datumet vara
  senare än idag.
- `savings_kr_20kwh` och `savings_kr_40kwh` får vara null båda två
  om kalkylen är för osäker. I annat fall är de ungefärliga besparingar
  inkl. **25 % moms endast på spotprisskillnaden**, jämfört med en
  klart angiven alternativ laddningsdag. Nätavgifter, energiskatt,
  effektavgifter och laddförluster ingår inte.
- Jämför i rapporten **praktiskt möjliga laddfönster**. 6 A enfas ger
  ungefär 1,38 kWh AC per timme, alltså cirka 14 kWh på tio timmar.
- `sources` är 1–6 publika källor med säkra `https:`-länkar utan
  querystring; källor måste faktiskt ha granskats. Lägg aldrig till
  åtkomsttoken, konton, VIN, körjournal eller personuppgifter.

### Skrivkontrakt för mcpsh47

När publiceraren är installerad och verifierad på Dell, kör en
**enstaka** mcpsh47 `query` med följande struktur och ersätt
`<JSON_FRAN_DAGENS_VERIFIERADE_ANALYS>` med giltig JSON utan markup:

```sh
sudo -n -u solinteg-evshadow /usr/bin/python3 \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_price_forecast_publish.py <<'XC40_FORECAST_JSON'
<JSON_FRAN_DAGENS_VERIFIERADE_ANALYS>
XC40_FORECAST_JSON
```

Kör inte om samma dag för att förfina en prognos utan att redovisa att
en ny prognoskörning skapas. Kontrollera att stdout innehåller
`"schema":"xc40_se2_price_forecast_v1"` samt exit 0.
Vid fel eller saknade data: skicka enbart ChatGPT-rapport med tydlig
brist, **ingen** dataskrivning, ingen ersättning av senaste giltiga JSON.

### Installation / verifiering

Kopiera enbart den fristående, testade pythonfilen till den root-ägda
isolationskatalogen. Ändra inga körande systemd-tjänster eller env-flaggor.

```sh
sudo install -o root -g solinteg -m 0644 \
  scripts/services/ev_price_forecast_publish.py \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_price_forecast_publish.py
sudo -n -u solinteg-evshadow test -r \
  /opt/solinteg/ev-shadow-runtime/scripts/services/ev_price_forecast_publish.py
python3 -m unittest scripts.tests.test_ev_price_forecast_publish -v
```

Ingen systemd-timer behövs: ChatGPT-jobbet är schemaläggaren.
Den privata databasen skapas först vid första riktiga körningen.
Vid rollback: pausa bara detta ChatGPT-jobb och ta vid behov bort
den publika JSON-filen; rör inga andra EV-/batteritjänster.
