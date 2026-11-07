# Bygga och granska dokumentationen

**Verktyg:** Zensical är versionslåst i [`requirements-docs.txt`](https://github.com/ermhawi/solinteg-controller-oss/blob/Sundsvall/requirements-docs.txt). Bygget arbetar enbart mot dokumentationsfiler och genererar en ignorerad `site/`-katalog. Det är **inte** en produktionsdriftsättning.

## Lokal installation på Ubuntu / WSL

Välj en isolerad Python-miljö, separerad från Solintegs batteri- och EVCC-tjänster:

```bash
cd ~/dev/github.com/ermhawi/solinteg-controller-oss
DOCS_VENV="${XDG_DATA_HOME:-$HOME/.local/share}/solinteg-docs-venv"
uv venv --python 3.11 "$DOCS_VENV"
uv pip install --python "$DOCS_VENV/bin/python" -r requirements-docs.txt
"$DOCS_VENV/bin/zensical" --version
"$DOCS_VENV/bin/zensical" build --strict
```

På Dell installerades `zensical==0.0.73` i denna användarseparerade miljö 2026-11-07 och länkades till `~/.local/bin/zensical`. Efter det kan `zensical build --strict` köras direkt i repo-roten om `~/.local/bin` finns i `PATH`.

Med standard-Python och pip kan samma versionslåsta krav installeras i en egen `venv` i stället för `uv`. Installera **inte** Zensical med `sudo pip` i systemets Python eller i produktions-venven.

## Lokal förhandsgranskning

```bash
zensical build --strict
zensical serve
```

Förhandsgranskningen visar aktuellt innehåll från `docs/` och arkivet, inte driftmiljön. Publiceringen i `.github/workflows/docs.yml` skapar en dokumentationsartefakt; den deployar inte Solintegs tjänster eller webbapp.

## Mermaid-diagram

Zensicals [officiella Mermaid-integration](https://zensical.org/docs/authoring/diagrams/) är aktiverad i `zensical.toml` via `pymdownx.superfences.custom_fences`. Diagram skrivs som `mermaid`-kodblock, renderas i webbläsaren och följer dokumentationens ljus/mörk-tema.

- [Komponenter och datavägar](architecture/system-overview.md)
- [Batteriprioritet, EVCC-beslut och felvägar](architecture/control-flows.md)

**Verifiering:** ett lyckat `build --strict` ska följas av kontroll att genererade sidor har `mermaid`-element och att diagrammen fungerar i webbläsaren. Den aktuella Zensical-teman placerar SVG i ett **stängt Shadow DOM** under `.mermaid`: `querySelector('.mermaid svg')` returnerar därför tomt även när diagrammet är korrekt renderat. Kontrollera det faktiskt ritade diagrammet, inte bara dess öppna DOM. Enbart giltig Markdown bevisar inte ett sant systembeteende.

## Rutiner för dokumentationsändringar

1. Kontrollera aktuella tjänster, källkod och [dokumenterade osäkerheter](known-issues.md). Skilj `föreslagen`, `implementerad`, `driftsatt` och `fysiskt verifierad`.
2. Uppdatera [driftöversikten](current-state.md), relevanta ADR:er och [kända brister](known-issues.md) när status faktiskt ändrats. Ange observationstid för operativa fakta.
3. Placera ersatta engångsexperiment i [historikarkivet](archive/index.md) och behåll tidigare länkar; påstå inte att ett ofullständigt test lyckats.
4. Kör `zensical build --strict`, granska Mermaid och kontrollera `git diff --check`. Ingen källa utanför `docs/` ska länkas som lokal Zensical-sida: använd GitHub-länk eller en motsvarande publicerad dokumentationssida.
5. Följ projektets [Git- och publiceringsregler](repository-layout.md). Publicera aldrig privata IP-adresser, hemligheter, faktura- eller råa hushållsmätvärden i Git.
