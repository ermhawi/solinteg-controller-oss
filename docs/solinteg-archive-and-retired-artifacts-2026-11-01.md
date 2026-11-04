# Arkiv och avveckling av Solinteg-testartefakter (2026-11-01)

**Status:** Genomförd engångsstädning på Dell/servern. Detta är
driftdokumentation; arkivets privata innehåll och krypteringsnyckel
är **inte versionshanterade**.

## Klassificering

| Klass | Åtgärd |
| --- | --- |
| Aktiv dispatcher, poller, EV/CT-vakter, databas/WAL och privata runtime-env | **Behåll**. Rör inte vid städning |
| Ordinarie nattliga lokala DB/env-säkerhetskopior | **Behåll och rotera** via befintlig `BACKUP_KEEP=21` |
| Färdigställt tillfälligt prov | Bevara terminal status lokalt som återaktiveringsspärr; krypterat USB-arkiv med kod, systemd-artefakter och evidens |
| Historiska app-/staging-/rollbackkopior | Bevara **en fullständig lokal rollbackkatalog**. Arkivera äldre källkod och privata historiska konfigurationsfiler krypterat. Skippa återskapningsbara `node_modules`, `.next`, `.venv` och cache |
| Incidentrapporter, ADR:er och regressionstester | **Behåll i Git** med historisk/avslutad status |
| Journal och kortlivade testloggar | Låt journald rotera; arkivera utvalda testloggar och radera bara explicit identifierade engångsfiler |

## Verifierad åtgärd 2026-11-01

- USB-monteringen kontrollerades mot faktisk blockenhet och
  filsystem före arkivering/radering. Denna installation använder
  `/mnt/media-stack` (USB/NTFS); `/mnt` ensamt är **inte** en
  säker arkivdestination.
- **35** gamla drifts-, rollback- och stagingkataloger togs bort
  från `/opt/solinteg`, vilket frigjorde **9 218,6 MiB**
  på systemdisken. Den aktiva `/opt/solinteg/app` och
  `app-pre-a13e559-20261026` finns kvar.
- **22** inaktiva SoC25-systemd-enheter (11 service + 11 timer),
  **fem** gamla root-library-kataloger och **två**
  inaktiva EV-grid-trial-enheter avinstallerades först efter
  arkivverifiering och kontrollerad terminal status.
  `systemctl daemon-reload` utfördes. Aktiva ordinarie
  säkerhetstjänster ändrades inte.
- **Sju** små historiska rotstatuskataloger behölls för
  spårbarhet och för att hindra återanvändning av engångs-ID:n.
  Äldre EV-grid-trial-statusmarkörer behölls.
- **49** verifierat arkiverade temporära testloggar och ett
  pausat gammalt kodutkast rensades. En separat databasavbild
  från 26 oktober kontrollerades med SQLite `quick_check`
  och verifierades **byte för byte** efter arkivering.
- De ordinarie 21 nattliga lokala databas- och miljöbackuperna
  och den aktiva `telemetry.db` lämnades orörda. Offsite-backup
  kunde inte verifieras. Journald har konfigurerad
  `MaxRetentionSec=14day` och effektiv `SystemMaxUse=1G`.

## USB-arkiv och återläsning

Arkivkatalog:

```text
/mnt/media-stack/archive/solinteg/2026-11-01-cleanup/
  README.txt
  cleanup-report.json
  SHA256SUMS
  trial-evidence.tar.zst.gpg
  legacy-deployment-sources.tar.zst.gpg
  legacy-telemetry-20261026.tar.zst.gpg
  ev-grid-trial-evidence.tar.zst.gpg
```

De fyra dataarkiven är GPG AES256-symmetriskt krypterade
eftersom NTFS inte kan garantera åtkomstbegränsning till privata
konfigurationer. Varje arkiv har dekrypterats och listats;
enhetsfiler, rootkod och statusar har dessutom SHA256-jämförts
mot ursprungliga filer. `SHA256SUMS` och
`cleanup-report.json` verifierar leveransen.

**För andra projekt:** [Gemensam agentrunbook för Bitwarden
Secrets Manager](https://github.com/ermhawi/engineering-standards/blob/main/docs/operations/bitwarden-secrets-manager.md)
dokumenterar åtkomst, projektval, SDK, säker metadatahantering och
verifiering av innehåll utan exponering i chatt/loggar.

**Verifierad nyckelbackup 2026-11-01:** GPG-lösenfrasen är sparad i
Bitwarden Secrets Manager som
`SOLINTEG_USB_ARCHIVE_PASSPHRASE_20261101`, i det befintliga
Secrets Manager-projektet som används av `ha-kube`.
Värdet lästes tillbaka via Bitwardens SDK och användes för att
**dekryptera och kontrollera** `trial-evidence.tar.zst.gpg` på USB,
**utan att den lokala nyckelfilen användes**. Den ursprungliga
`/root/.config/solinteg-archive/passphrase` är fortsatt root-ägd
med `0600`. Bitwarden-åtkomsten måste också kunna återställas
oberoende av servern. Arkivet är inte en komplett backup av
hela servern. Lägg **aldrig** en okrypterad nyckel på samma USB-disk.

För läsbarhetskontroll på ursprunglig server:

```bash
cd /mnt/media-stack/archive/solinteg/2026-11-01-cleanup
sha256sum -c SHA256SUMS

sudo gpg --batch --quiet --pinentry-mode loopback \
  --passphrase-file /root/.config/solinteg-archive/passphrase \
  --decrypt trial-evidence.tar.zst.gpg |
  zstd -dc | tar -tf -
```

Extrahera **alltid** först i en separat tom granskningskatalog,
aldrig direkt över `/opt/solinteg/app` eller `/etc/systemd/system`.
Äldre källkod utan `node_modules` är **inte en startbar rollback**.
Den lokalt behållna fulla kopian är inte separat start- eller
restaureringstestad i denna åtgärd.

## Uppföljande EV-shadow-avveckling 2026-11-04

Användaren valde att behålla endast **faktiska mätkällor för live**
och avveckla de separata shadow-tjänsterna. USB-monteringen
verifierades som `/dev/sdb1`, NTFS/`fuseblk`, och en ny GPG
AES256-krypterad leverans skapades:

```text
/mnt/media-stack/archive/solinteg/2026-11-04-ev-shadow-retirement/
  ev-shadow-units-and-source.tar.gpg
  MANIFEST.json
  SHA256SUMS
  README.txt
```

**SHA-256** för arkivet:
`c3be49228b6e777b5c11780cd15ad664b4c6b6fa43f04e16b8194846ad5a4b67`.

Manifestet omfattar **13 filer**: fem installerade EV-shadow-/legacy
watchdog-systemd-enheter, fem separata rootägda observationsmoduler,
en inaktiv EV-shadow-planerare, historiktjänstens tidigare unitfil
och den historiska privata shadow-JSON:en. GPG-dekryptering lyckades;
**samtliga 13 filer återlästes byteexakt** och SHA kontrollerades innan
något togs bort. Enligt arkivpolicyn ligger endast krypterad data
på USB, ingen klartextnyckel. Lokal rootägda nyckelfil och
tidigare verifierad extern Bitwarden-kopia rördes inte.

Efter verifierad `inactive/disabled`/ej beroende av annan enhet
avinstallerades följande **fem** unitfiler:

- `solinteg-ev-grid-solar-shadow.service/.timer`
- `solinteg-ev-shadow.service`
- `solinteg-ev-grid-priority-watchdog.service/.timer`

Fem inaktiva rootmoduler under
`/usr/local/libexec/solinteg-ev-grid-solar-shadow`, den inaktiva
`ev_shadow_planner.py` och dess gamla output-JSON avvecklades också.
Exakt **tolv** filer togs bort och rootkatalogen raderades efter
fil-/SHA-kontroll. `solinteg-ev-history.service` har enbart fått
sitt gamla `After=... solinteg-ev-shadow.service` borttaget och
`systemctl daemon-reload` kördes **utan** att starta om historiken.
En terminal rootägd statusfil bevarades:

`/var/lib/solinteg-evshadow-rollback/ev-shadow-retirement-20261104.status.json`.

**Bevarat och aktivt:** `solinteg-ev-observer` (Waveshare/EVCC-Ict2),
`solinteg-ev-history` (synkron historik), `solinteg-ev-ha` (Volvo),
live-dispatch, poller, broker och vanlig watchdog. De behövs för
mätning och uppföljning, **inte för parallell shadow-styrning**.
Äldre rootgrind/tombstones och nödvändig lokal rollback ligger kvar;
okänt/operatörsägt material raderades inte.

Se [aktuell ADR för EV/L3 live och panel](architecture/adr-2026-11-04-ev-l3-live-grid-first-and-ui.md).

## Definition of Done för kommande engångsprov

**Agentansvar enligt engineering-standards v1.3.0:** följ
[den normerande artefaktpolicyn](https://github.com/ermhawi/engineering-standards/blob/v1.3.0/docs/artifact-hygiene.md)
tillsammans med Git-hygien. En agent som skapar testtimers, tillfällig
root-kod, installationskopior eller loggar ska också inventera och
avveckla sina **egna** resurser vid avslut. En avstängd timer är inte
avinstallerad; ett rent Git-repo är inte ett rent driftssystem.
Äldre/okända eller användarägda resurser bevaras till separat
godkännande. Ingen ny automatisk destruktiv timer aktiveras.

Ett tillfälligt prov är inte färdigt enbart för att timer är
`disabled` eller för att Git är rent:

1. Verifiera fysisk återställning, ordinarie dispatcher,
   säkerhetsvakter, färska mätvärden och **terminal**
   `completed`/`aborted`-status.
2. Bevara provets terminala root-status/ägarskaps-ID som
   återaktiveringsspärr. Arkivera verklig installerad
   kod, systemd-enheter, relevant journal och incidentbevis
   med checksummor. Stoppa vid saknad USB-mount eller
   ofullständigt/icke återläsbart arkiv.
3. Avinstallera **endast ägda, inaktiva** test-`.service` och
   `.timer`; kör `systemctl daemon-reload`. Ta bort
   rotägd testkod och kända scratchfiler först efter
   arkiv-/hashkontroll. Behåll okända eller pågående arbeten.
4. Bekräfta att ordinarie tjänster fortfarande körs och
   att inga nya Modbus-skrivare, timers, arbetsgrenar eller
   worktrees lämnats kvar.
5. Uppdatera driftdokumentation och ADR och slutför
   Git-hygien på den **fristående** `Sundsvall`-grenen.
   Mergning till `main` eller upstream ingår **inte**.

USB-diskens tillgänglighet och externa backupstatus ska granskas
vid varje framtida arkiveringsåtgärd. Automatisk destruktiv
städning får inte köras utan en fail-closed ägar- och
arkivverifiering.
