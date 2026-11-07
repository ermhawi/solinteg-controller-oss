# K03 – Python-lint, format och säkerhet: första baslinjen

**Status 2026-11-07:** endast läsande rapportläge, `tested`. Ruff **0.17.4**, körd med användarseparerad `uvx`-cache över `scripts/services`, `scripts/tools` och `scripts/tests`. Ingen ändring i dessa filer eller i installerad runtime.

```bash
uvx --offline --from ruff==0.17.4 ruff check \
  --select E4,E7,E9,F821,F822,F823,B006,B007,S104,S105,S106,S307,S608 \
  --output-format json scripts/services scripts/tools scripts/tests \
  > /tmp/solinteg-ruff-baseline-20261107.json
uvx --offline --from ruff==0.17.4 ruff format --check \
  scripts/services scripts/tools scripts/tests \
  > /tmp/solinteg-ruff-format-20261107.txt 2>&1
```

| Regel | Fynd | Åtgärd |
| --- | ---: | --- |
| S608 – möjligt SQL-strängbygge | 24 | **Granska separat** före eventuell parametrisering; inte verifierad SQL-injektion |
| B006 – mutabelt standardargument | 1 | Riktad buggranskning, regressionstest först |
| E402 | 89 | Flertalet testimporter; bibehåll nuvarande struktur |
| E701/E702 | 11/10 | Ingen massformattering |
| E731/E741 | 2/4 | Lokala småändringar endast om verifierade |
| B007 | 3 | Låg prioritet |
| F821/F822/F823 | 0 | Ingen träff i vald omfattning |

**Totalt 144 fynd i 53 filer**. `ruff format --check` rapporterade **218 filer att formatera**, endast en uppfyllde hela formatet. Dessa rapportfynd är inte bevis för exploaterbara säkerhetsfel. Både lint och format hade exit 1 **som förväntat för rapportläge**. Inga skrivande `ruff format`-kommandon har körts.

**Nästa gradvisa steg:** välj en kritisk filgrupp, skapa regression för varje S608/B006 som faktiskt är ett fel, ändra minsta diffen och gör först därefter en lokal CI-regel blockerande. Rapporterna ligger privat under `/tmp` och ska inte checkas in; relevanta fynd sammanfattas här.