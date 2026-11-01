# ADR 2026-11-01: USB-arkiv för terminala driftsprov och versionsspårning

**Status:** Antaget och utfört som en kontrollerad engångsstädning.
Löpande automatisk radering är **inte** driftsatt.

## Kontext

Tillfälliga SoC25- och EV-prioritetsprov återställde driften vid
avbrott men lämnade root-kod och systemd-enheter installerade.
Dessutom fanns 36 gamla app-/staging-/rollbackkataloger som
upptog omkring 9,5 GiB. Historiska incident- och testdata behöver
bevaras även om filerna inte ska in i ett offentligt Git-repo.

## Beslut

Använd den **fysiskt kontrollerade monterade USB-disken** som ett
separat, krypterat historiskt arkiv. NTFS ger inte tillräckligt
privat filägarskap: GPG-symmetrisk AES256-kryptering och SHA256
krävs för privata miljö-/provdata. All arkivering måste
misslyckas säkert om monteringen saknas, innehållet inte kan
dekrypteras/läsas tillbaka eller säkerhetsstatus inte är terminal.

Bevara **en komplett lokal rollbackkatalog** som snabb reserv.
Arkivera äldre källkod utan återskapningsbara
`node_modules`, `.next` och `.venv`. Spara unik gammal
SQLite-avbild separat, byteverifierad; behåll aktiva databaser och
den vanliga backuprotationen orörda.

Avinstallera inaktiva engångs-`systemd`-enheter och rotägd kod
först efter verifierad arkivering, men behåll terminala
rotstatusmarkörer som spärr mot oavsiktlig återaktivering.
Behåll historiska ADR:er, tester och incidentrapporter i Git.

## Konsekvenser och återstående risk

Engångsstädningen frigjorde **9 218,6 MiB** lokalt,
avinstallerade **24** gamla test-`systemd`-enheter och fem
gamla rotbibliotek. Aktiva dispatcher-/poller-/säkerhetsvakter
fick inte nya Modbus-skrivare eller ändrade trösklar.

Den enda GPG-återläsningsnyckeln lagras hittills på serverns
systemdisk, root-`0600`. **Separat säker förvaring av nyckeln, exempelvis
i Bitwarden, återstår** innan USB-arkivet utgör oberoende
katastrofåterställning. Att en äldre katalog arkiverats
utan byggberoenden betyder dessutom att den inte längre
kan startas som en omedelbar rollback.

En framtida automatiserad cleanup måste ha en testad,
idempotent ägar- och checksummekontroll; manuellt verifierade
historiska raderingar får inte generaliseras till
godtyckliga filnamn eller aktiva systemd-enheter.

Se [genomförandelogg och runbook](../solinteg-archive-and-retired-artifacts-2026-11-01.md).
