# Sanerad releaseinventering och driftsparitet (B03, R01, R02, R07)

**Status 2026-11-07: verktyg implementerat och offline-/read-only-testat, ingen release.** `scripts/tools/release_inventory.py` använder en fast modul-/unitlista och tillåter endast utvalda låg-entropiflaggor. Läser systemd/proc/filhashar; **inget nätverksanrop, ingen Modbus/EVCC och ingen tjänsteåtgärd**. Utdata innehåller *endast basnamn* för unit, drop-ins, exekverbar fil och entrypoint, aldrig råa `ExecStart`-argument, fullständiga `Environment`, privata paths, adresser eller godtyckliga flaggvärden.

## Läsande körning och privat evidens

```bash
cd ~/dev/github.com/ermhawi/solinteg-controller-oss
umask 077
mkdir -p ~/.local/state/solinteg-release-inventory
python3 scripts/tools/release_inventory.py --rollback-ref=R08-pending \
  > ~/.local/state/solinteg-release-inventory/snapshot.json

# Med enbart läsbehörighet till installationskatalog/proc:
# kör den redan granskade versionen under en läsbehörig identitet.
# Privilegierad körning får inte vara normal driftprocedur innan scriptet
# har granskats och kopierats till en root-ägd, skrivskyddad version.
```

För verifiering av kontrollern kan ett *granskat* exemplar köras med `/usr/bin/python3 -I -B` under en identitet som får läsa /opt- och /proc-data. Ingen färsk systemd-omstart, installation eller fysisk sond behövs. En obehörig körning är avsiktligt **incomplete**, inte `equal`. Tilläggsargument: `--repo`, `--installed`, `--ev-runtime`, `--test-summary` (enbart whitelistade testantal), `--rollback-ref` (opakt ID utan path), `--compare-json` (tidigare snapshot).

`--compare-json <tidigare.json>` skriver en sanerad diff, **ingen automatisk synkronisering**. Fältet `status` jämför två observationer och ignorerar tidsstämpeln. `manifest_gate` blockerar däremot releaseberedning vid någon okänd/otillgänglig installerad fil, faktisk käll-/installationsavvikelse, ändring sedan föregående snapshot eller ocommittad källkod. Kod 0 = paritet/grindpass, 5 = blockerad releasegrind. **Pass** är aldrig ett tillstånd att driftsätta; separata GO/NO-GO-grindar och fysiskt godkännande kvarstår. Kör kontroll före *och* efter en separat godkänd release, lagra båda rapporterna privat och jämför selektivt.

## Verifierad provkörning 2026-11-07

- Två read-only-snapshots: `~/.local/state/solinteg-release-inventory/2026-11-07-{a,b}.json`, mode 0600 i katalog mode 0700. Sanerad diff `2026-11-07-compare.json`, SHA-256 `6ae3f394fba20e62dd51649197afc0ab650bb6554672d40812bf94a2ec0013bd`. Dessa är **privata driftbevis utanför Git**, inte återställningsbackup.
- `Sundsvall` på `98be341` (arbetskatalogen smutsig). **18/18** käll-/driftmoduler läsbara: 11 `equal`, 7 `different`. Avvikande: `dispatch_loop.py`, tre broker-filer (`modbus_broker.py`, `modbus_broker_control_service.py`, `modbus_broker_ipc.py`) samt `optimizer.ts`, `plan.ts` och `prices.ts`. Ingen av dessa avvikelser har förklarad releasekandidat ännu; releasegrinden är **BLOCKED**, exit 5.
- **11** unit-/timerobjekt inventerade och **7** whitelisted flaggor. Effektivt på aktiv dispatcher: `CONTROL_ARMED=1` via privat envfil, `MODBUS_SOURCE=broker_transaction` via `30-transactions.conf`, `DISPATCH_ENERGY_PRIORITY=1` via `40-energy-priority.conf`. Broker har transaktionsflagga `1` via `30-transactions.conf`. Hold-probe-opt-in och reserv-hold-verifiering saknas. Bara *flaggnamn och tillåtna värden* bevarades.
- `python3 -m unittest discover -s scripts/tests -p 'test_release_inventory.py'`: 12/12 godkända (ursprungligen 11, sedan EV-importtäckning), även maskering av hemliga argv/env, deterministisk diff, okänd status och blockerad releasegrind. `zensical build --strict`: godkänd. **Status:** `tested`/read-only, inte installerad release och inte `physically_verified`.

## EVCC som separat releasenyckel (R05, 2026-11-07)

**Runtime:** `/opt/solinteg/ev-shadow-runtime` är en **egen fysisk Python-installation**, oberoende av `/opt/solinteg/app`. Read-only `systemctl show` gav följande aktiva/påtimer-kedjor: `solinteg-ev-observer.service` → `evcc_observer.py`, `solinteg-ev-history.service` → `ev_history.py`, `solinteg-ev-ha.service` → `ha_ev_session.py`, och `solinteg-ev-price-controller.timer` (active/waiting) → `solinteg-ev-price-controller.service` → `ev_price_controller.py` (inactive/dead mellan timerkörningar; inte bevis för att FC10 utförts).

AST-inventering av de fyra entrypointsen och deras transitive *lokala* Python-importer gav **12 moduler**, samtliga **byteidentiska mellan repo och EV-runtime**, direkt verifierat via privilegierad men skrivskyddad SHA-256-läsning:

| Grupp | Lokala Python-moduler | Paritet |
| --- | --- | --- |
| Entrypoints | `ev_price_controller.py`, `evcc_observer.py`, `ev_history.py`, `ha_ev_session.py` | 4/4 equal |
| Direkta/transitiva moduler | `ev_price_empirical_session.py`, `ev_price_pause_owner.py`, `ev_price_policy.py`, `ev_volvo_smart_charging.py`, `evcc_ascii.py`, `evcc_cable_tracker.py`, `ev_charge_end.py`, `common.py` | 8/8 equal |

Det statiska EV-manifestet i `scripts/tools/release_inventory.py` omfattar nu alla tolv och `test_ev_manifest_covers_active_entrypoints_and_all_local_imports` bevakar nya lokala importberoenden (12/12 releaseinventeringstester godkända). Äldre privata snapshots i avsnittet ovan innehåller bara sex EV-moduler (18 moduler totalt); de är **inte** retroaktivt omskrivna. Nästa snapshot använder **24 moduler** totalt. PyPI/systempaket och dynamiskt inlästa externa filer är **inte** hashverifierade här; återstår i releasekandidatens R06-beroendemanifest. `R05` bevisar inte att EVCC styrts fysiskt; FC10 härledning och manuell paus hör till E01/E03.

`R08-pending` är **inte** ett verifierat återställningsunderlag. Befintlig installation får inte ersättas med Git-version; de sju avvikelserna behöver motiverad R03/R04-klassning, och skyddade preexisting ändringar måste återföras till reproducerbar källversionskontroll före release. Ingen privat snapshot eller hemlighet ska committas.
