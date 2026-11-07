# A01/A02 · Python-entrypoints, källkod och runtimeparitet

**2026-11-07 – `tested`/read-only, aldrig en raderingslista.** Maskinläsbart underlag: [83 individuella modulposter](python-entrypoint-inventory-2026-11-07.json), framtagna med `scripts/tools/python_reachability.py --sudo-read`. Verktyget granskar endast AST-källtext, statiska och bokstavliga dynamiska importer, test-/tool-/deploy-referenser, läsande `systemctl show` och SHA-256 med **opt-in** `sudo -n /usr/bin/sha256sum` för otillgängliga installerade Python-filer. Det skriver **inte** till `/opt`, ändrar inga units och ansluter aldrig till inverter/EVCC. Inga raw systemd-argv, envvärden, privata IP-adresser eller råkodsdelar finns i rapporten.

| Kontroll | Läsande resultat och tolkning |
| --- | --- |
| Källans tjänstemoduler | **83** Python-filer under `scripts/services`, var och en med relativ path, domänägare (83/83 tilldelade), funktion (55 granskade källdocstring-rubriker / 28 försiktiga modulnamnsfallback), importgraf, dynamiska importer, direkta tester/verktyg/deploy-referenser, eventuell systemd-kedja, möjlig fysisk effekt och Git/backupstatus |
| Identifierade systemd-tjänster | **22 `loaded` serviceobjekt** hittades av read-only `systemctl list-units`; **15** namngivna Python service-entrypoints under `scripts/services` matchar inventeringens källmoduler (exklusive t.ex. webbserver) |
| Statiskt nåbara moduler | **42** från dessa 15 entréprogram (inaktiva oneshot kan ha timerägare och ska inte kasseras) |
| Statiskt ej nåbara | **41** nya kandidater (tidigare baslinje 38 av 79 före tillägg av nya källmoduler och bredare unitgenomgång). Ej nåbar från den här grafen betyder **inte** oanvänd i drift |
| Klassificering | **42 live**, 6 manual, 19 recovery-reserve, 6 historical, **10 unknown**. De sistnämnda är avsiktliga utredningsposter, inte godkända för upprensning |
| Installerad huvudsökväg | **25 ok / 58 missing** enligt läsande privilegierad SHA-256-inventering under `/opt/solinteg/app`; alla resultat har filhash när läsbar |
| Installerad separat EV-runtime | **22 ok / 61 missing** under `/opt/solinteg/ev-shadow-runtime`. Detta är en separat releasenyckel enligt R05; fler installerade EV-verktyg behöver fortfarande manuell kvalificering |
| Git- och arbetsstatus | `source_git_backup` anger spårad/ospårad per modul. `last_execution=not_verified` tills verklig exekveringslogg kan bindas till modul och installerad hash. **Ingen fil har `deletion_proposed=true`.** |

## Osäkra 10 – BEHÅLL för fortsatt bedömning

`backup_offsite.py`, `control_health_contract.py`, `ev_grid_priority_idle_strategy.py`, `ev_grid_priority_watchdog.py`, `ev_grid_trial_watchdog.py`, `ev_l3_metered_policy.py`, `ev_solar_policy.py`, `native_soc_safety_evidence.py`, `safety_alert_contract.py`, `weather_poller.py`.

- Nya `control_health_contract.py`, `native_soc_safety_evidence.py` och `safety_alert_contract.py` är **avsiktligt source-only**, offline-testade framtida kontrakt; ska *inte* raderas bara för att de ännu inte har en systemd-ägare.
- `weather_poller.py` kan vara anropad från schemaläggare/manuell körning; kontrollera timer/cron och eventuell separat webbkedja före beslut.
- EV-prioritet/idle/L3-/trial-watchdogfilerna är policy- eller övergångsprober där dokument-/systemdreferenser och fysiska beroenden måste stämmas mot pågående EVCC-kvalificering.
- `backup_offsite.py` kan vara manuellt backup-/recoveryflöde och får inte förväxlas med borttagen applikationskod.

**Definition of Done för detta inventeringssteg:** 83/83 individuella poster, 41/41 ej nåbara kandidatposter, 83/83 domänägare, 55 docstringbaserade syftesrubriker och 28 konservativa fallback, verkliga aktiva/inaktiva systemd-referenser, statiska och bokstavliga dynamiska importer, test-/shell-/deployreferenser, separat drifthash samt källspårbarhet. **Kategori `unknown` får aldrig kombineras med `deletion_proposed`** (0 överträdelser). Automatiserad inventering kan inte bevisa att det inte finns oläsbara externa cron-entrypoints, computed imports, dynamiska subprocess-anrop, installerade verktyg utanför `scripts/services`, oavsiktlig direkt-TCP eller historiska återställningsbehov. Därför är **A06/A07 fortfarande öppna**, inga filer tas bort och A05 (TS-nåbar kodgranskning) får en separat verifieringscykel.

**Reproduktion:**

```bash
python3 -m unittest discover -s scripts/tests -p 'test_python_reachability.py'
python3 scripts/tools/python_reachability.py --sudo-read \
  --output /tmp/solinteg-python-entrypoint-nytt.json
```

`--sudo-read` kräver att operatören tillåter läsning av redan installerade modulhashar, **aldrig råkod eller miljöfil**. Utan flaggan är oläsbara driftmoduler markerade `not-readable`, inte påhittat `equal`. `--output` vägrar skriva över existerande rapport, och skillnader mellan rapporter ska förstås i förhållande till commits och faktisk installerad kod.
