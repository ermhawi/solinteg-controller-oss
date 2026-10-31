# Kanoniskt utvecklingsrepo på Dell

**Beslut 2026-10-28:** All ny Solinteg-utveckling, testning, Git-hantering
och MCPSH/Honcho-reposkopad kontext ska utgå från **en** checkout:

```text
~/dev/github.com/ermhawi/solinteg-controller-oss
branch: Sundsvall
origin: ermhawi/solinteg-controller-oss
upstream: hspolander/solinteg-controller-oss (extern originalkälla)
```

`hspolander` i **GitHub-remoten** är fortfarande korrekt: den anger
ursprungsprojektet. Den betyder **inte** att vår lokala checkout under
`~/dev/github.com/hspolander/` ska användas. Ta inte fram fler kloner
eller Git-worktrees utan att de uttryckligen behövs och dokumenteras.
Välj GitHub CLI `gh` för GitHub-operationer.

## Utveckling är inte drift

| Plats | Roll | Git-checkout |
| --- | --- | --- |
| `~/dev/github.com/ermhawi/solinteg-controller-oss` | Kanonisk källkod, branch `Sundsvall` | Ja |
| `/opt/solinteg/app` | Befintlig produktionsinstallation för husbatteri och webb | Nej, separat driftsättning |
| `/opt/solinteg/ev-shadow-runtime` | Isolerad, skrivskyddad Volvo/Waveshare/skuggdrift; privata JSON- och historikfiler | Nej, root-ägda distributionsfiler |
| `/usr/local/libexec/solinteg-ev-grid-solar-shadow` | Rootägda skript för separat read-only EV/L3-observatör | Nej, inga skrivrättigheter till växelriktaren |
| `~/dev/github.com/hspolander/solinteg-controller-oss` | **Avvecklad 2026-10-30; källkod, refs och privat miljö arkiverade** | Nej, katalogen borttagen |

Installationskatalogerna uppdateras **inte automatiskt** av en commit
i utvecklingsrepot. Flytta inte över live-konfiguration, systemd-enheter
eller aktiverad styrning när en klon städas bort.

## Historik och säker avveckling av gamla klonen

Den äldre klonen stod på `Sundsvall` vid commit `11c7fbd`, vilket är
en förfader till vår kanoniska branch. Samtliga åtta senare EV-commits
finns i `ermhawi`-checkouten. Båda Git-arbetskatalogerna var rena
vid verifieringen 2026-10-28; den äldre hade inga omergade lokala
brancher.

Det fanns en **unik ignorerad `.env.local`** i den äldre klonen,
inklusive lokal styrkonfiguration och nycklar. Den har arkiverats
separat med restriktiva filrättigheter under:

`~/backups/solinteg-canonical-migration-20261028/`

Den verifierade säkerhetskopian innehåller Git-bundle med samtliga
refs, en källkods-/`.git`-arkivfil (inklusive reflog/oincheckade
Git-objekt) samt originalets `.env.local`. Backuperna har rättigheter
0700 för katalogen och 0600 för filer. Inga hemligheter får kopieras
till Git eller klistras in i agent-/Honcho-minne. **Kopiera inte
`.env.local` till den kanoniska katalogen utan en separat granskning
av bland annat `SOLINTEG_CONTROL_ARMED`**.

**Avvecklad 2026-10-30:** Den gamla klonen var ren på `11c7fbd`,
utan stashes, omergade lokala arbetsgrenar eller extra worktrees.
Inga aktiva processer hade arbetskatalog eller öppna filer där och inga
systemd-enheter pekade på katalogen. Både Git-bundlen, källkodsarkivet
och säkerhetskopian av dess privata `.env.local` verifierades;
miljöfilens SHA256 var identisk. Därefter avvecklades den separata
klonen. Ett privat kvitto anger backupfiler och ursprunglig commit.
**Den kanoniska `ermhawi`-checkouten, `/opt/solinteg`, systemd och
hårdvarustyrning lämnades oförändrade.** Återskapa bara den äldre klonen
från privat backup om ett dokumenterat återställningsbehov uppstår.

## Git-hygien och publicering

**Permanent branchregel (beslut 2026-10-30):** `Sundsvall` ska **aldrig mergas**
till `main` eller till `hspolander`-upstream. Efter granskning publiceras endast
den självständiga branchen på `origin/Sundsvall` i den egna forken. Alla verkliga
nätverksadresser och kunders fakturavolymer saneras också ur **opublicerad
commithistorik** före push; publicerade exempel använder syntetiska värden.

Efter varje kodändring: rapportera kort repo, branch, ocommittade
och orelaterade filer, lokala omergade brancher och kvarvarande
worktrees. Commita bara arbetsrelaterade filer och radera aldrig
andras orelaterade filer eller worktrees på eget initiativ.

**Status 2026-10-30:** 26 granskade och sanerade commits har publicerats med
en vanlig fast-forward-push till `origin/Sundsvall`, **utan merge** till
`main` eller `upstream`. Den opublicerade historiken skrevs om i en
isolerad klon så att verkliga interna IP-adresser och fakturerade
förbrukningsvolymer inte finns kvar i publicerade commits. Gitleaks gav
0 fynd i det granskade intervallet. Den ursprungliga historiken och
`GRANSKNING-2026-10-28.md` har verifierade privata arkivkopior
**utanför** Git-utcheckningen; den gamla lokala ignore-regeln togs bort.
Publika `.service`-filer och miljöexempel innehåller syntetiska
exempeladresser som måste anpassas enbart i privata installationskopior.

Detta var en **Git-publicering, inte en driftsättning**: inget i den
ARMED Solinteg-dispatchern eller EVCC:s fysiska strömgräns ändrades.

Honcho ska behandla detta beslut som en **uttrycklig ersättning**
av det tidigare beslutet att `hspolander` var primär lokal checkout.
