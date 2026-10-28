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
| `/opt/solinteg/ev-shadow-runtime` | Isolerad, skrivskyddad Volvo/Waveshare/skuggdrift | Nej, root-ägda distributionsfiler |
| `~/dev/github.com/hspolander/solinteg-controller-oss` | **Utgående historisk klon** | Ja, men får inte användas för nytt arbete |

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

Borttagning av den äldre klonen är **tillfälligt blockerad** tills
befintliga interaktiva processer med arbetskatalog där (ett zsh-skal
och en Claude-session observerades 2026-10-28) har avslutats eller
bytt arbetskatalog. Döda inte dessa processer automatiskt, och
radera/flytta inte deras aktiva arbetskatalog. När processen är fri:
verifiera `git status`, senaste commit, eventuellt nytt ocommittat
arbete, att backupen är läsbar och att tjänster inte refererar till
vägen. Avveckla då endast den gamla klonen utan att röra
`/opt/solinteg`, GitHub eller våra andra repon. Ingen automatisk
`git push`, deploy eller borttagning är en del av detta beslut.

## Git-hygien och publicering

Efter varje kodändring: rapportera kort repo, branch, ocommittade
och orelaterade filer, lokala omergade brancher och kvarvarande
worktrees. Commita bara arbetsrelaterade filer och radera aldrig
andras orelaterade filer eller worktrees på eget initiativ.

De åtta tidigare EV-commitsen efter `11c7fbd` finns ännu enbart
lokalt på `Sundsvall` vid detta beslut. Granska incheckade
privata IP-adresser/konfiguration och den lokalt ignorerade
`GRANSKNING-2026-10-28.md` **innan** eventuell push eller
produktionsdriftsättning. Den rapporten finns i den kanoniska
klonen och ska bevaras utanför Git tills fynden behandlats.

Honcho ska behandla detta beslut som en **uttrycklig ersättning**
av det tidigare beslutet att `hspolander` var primär lokal checkout.
