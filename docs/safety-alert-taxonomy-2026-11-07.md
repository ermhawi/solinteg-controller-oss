# T03 · Kritiska larmhål, deduplicering och operatörsansvar

**Source-only kontrakt 2026-11-07, inte inkopplad till befintlig notifiering.** Det är en **inventering** och en testbar, ren dedupmodell, inte en ny fysisk health-supervisor. Källa: [S01-felmatris](architecture/failure-mode-matrix-2026-11-07.md), [S03-hälsokontrakt](architecture/adr-2026-11-07-control-health-contract.md), [Wi-Fi-körläge](operations/wifi-watchdog-readonly-2026-11-07.md), EVCC:s durable pause-owner och broker lease.

| Kritisk feltyp, ny stabil nyckel | Indikator / provenienskrav | Ansvarig och åtgärd |
| --- | --- | --- |
| `solinteg:broker_telemetry_invalid` | stale/nekad broker-poller quality, **inte** bara process-live | Solintegoperatör: stoppa nya skrivbeslut, verifiera FC03-/registerägare |
| `solinteg:wifi_recovery_escalating` | drivrutinsindikator + eskalationssteg i Wi-Fi-watchdog; process-logg ensam kan vara försenad | Host-/nätoperatör: kontrollera reset/readback innan reboot |
| `solinteg:dispatcher_heartbeat_missing` | oberoende process+heartbeat-observatör (inte bara timestamp) | Solintegoperatör: process, apply-readback och fysisk EMS måste kontrolleras |
| `solinteg:broker_owner_unknown` | lease/TCP-socket-ägare okänd, misslyckad fencing | Solintegoperatör: inhibera nya skrivningar och verifiera single-owner |
| `solinteg:soc_under_reserve` | färsk oberoende SoC och ekonomisk reserv (skilj från BMS-EndSOC) | Energioperatör: verifiera färsk BMS-/registermätning, recovery och reserv |
| `solinteg:ems_state_uncertain` | registerreadback eller fysisk effekt stöder inte begärd mode/last | Energioperatör: ingen blind Auto, kontrollera ägare och säkert rollbackläge |
| `solinteg:ev_pause_owner_uncertain` | durable owner `pending/releasing/revoked`, kabel-epoch eller FC10-resultat okänt | EV-operatör: inhibera automatisk resume, kontrollera kabel/ägarskap/FC10 |

**Deduplicering:** `scripts/services/safety_alert_contract.py` kräver känd signal och exakt proveniens; `dedup_key=solinteg:<code>` är stabil oberoende av tick, PID och feltext. `last_state=active` plus fortsatt fel genererar **inget nytt larm**. `None` = `unknown` och får aldrig kvittera ett aktivt larm; uttryckligt återhämtat `False` ger `clear_active=True`. Avvikande okända signalkoder och falska källa avvisas. Detta är en **ren klassifikator**. Beständig larmepisod, notifierings-/ack-lager, TTL och fysisk status måste kopplas på i separat T04/S03 innan operatören faktiskt får larm; inga anspråk på installerad end-to-end-observation.

**Test/evidens:** `scripts/tests/test_safety_alert_contract.py` **5/5** (sju kritiska fall, stabil identitet, upprepat larm, unknown/recovery, verifierad proveniens). Inga sockets eller skrivbara filer. **Status:** `tested` för taxonomy/dedup och `source` för operatörsroll, inte `installed` eller `physically_verified`.

**Kvarvarande larmhål:** faktisk push/notifiering av producerat signalvärde, kvittens ägd av operatör och persistent episod, separat extern larmväg vid Dell-strömavbrott, observation av fysisk EMS vid blockerad broker. Dessa är T04/S08/V-uppgifter och får inte räknas som levererat skydd av T03.