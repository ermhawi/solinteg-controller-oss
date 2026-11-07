# T04 · Hälsoövervakningens poller-/weatherfreshness: minimal extraktion

**2026-11-07 – `tested/source`, inte installerad.** Första avgränsade gruppen i den cirka 1 000 rader stora `scripts/services/healthcheck.py` har flyttats till **`scripts/services/healthcheck_freshness.py`** utan att ändra styr- eller larmpolicy. Exakta legacy-funktionsnamn `check_poller_stale` och `check_weather_stale` finns kvar som små kompatibilitetswrappers. Den nya modulen tar `safe_scalar`, notifieringens priority och envgrundade stale-trösklar som parametrar; den läser endast SQLite och har inga systemd-/FC06-/EVCC-skrivvägar.

| Given | Tidigare larmfingertopp/aktivitet | Verifierad ny källa |
| --- | --- | --- |
| Inga `readings` eller tabell saknas | `poller_no_data`, hög prioritet, samma titel/text | **Byteidentisk Tuple**, including notification wording |
| Senaste `readings` 301 s gammal | `poller_stale`, hög prioritet, 301-sekunders text | **Samma tuple**, ingen förändrad stale-bedömning |
| Senaste `readings` vid exakt 300 s eller 10 s gammal | Inget larm | Fortfarande `None` |
| Inga `weather` eller tabell saknas | `weather_no_data`, låg prioritet, samma titel/text | **Samma tuple** |
| `weather` stale vid 1801 s | `weather_stale`, låg prioritet | **Samma tuple och nonurgent-text** |
| Weather-tröskel `0` (sensor avaktiverad) | Inget nytt weather-larm; `run_checks` kan fortfarande göra normal resolve-sweep | Fortfarande `None`, inga sideffekter |
| Upprepad poller-stale under cooldown | `should_alert` för samma fingerprint `str(priority)` ger `False` | Legacy-koden och fingerprint är **orörda**, verifierat i test |

**Testbevis:** `scripts/tests/test_healthcheck_freshness_parity.py` **4/4** in-memory SQLite-golden fall med larm-ID, prioritet och exakt text, 300/1800 s gränser, saknad tabell, disabled weather, plus cooldown/resolution-fingeravtryck. Dessutom **127/127 befintliga healthcheck-/related tester** fortsätter att passera, totalt **131/131**, utan någon fysisk testmiljö. Källan är mindre duplicerad och de två oberoende mätålderskontrollerna kan testas utan att initiera produktionens notifiering.

**Begränsningar:** Endast **en tydligt avgränsad delgrupp** har extraherats i denna iteration; övriga healthcheck-kontroller, notifiering, `resolve`-sweep och producentkontrakt är oförändrade. Det är inte en ny end-to-end-supervisor eller fysisk failsafe. Följande grupper kan flyttas i senare små iterationer med motsvarande golden-tests, men någon global 1 000-radersrefaktorering har inte genomförts. Driftkod och systemd har inte modifierats.
