# ADR 2026-11-05: GitHub OAuth2 utifrån, betrott internt för Solintegs webb och API:er

**Status: driftsatt på Dell, 2026-11-05.** Gäller hela
Solintegs ordinarie webbapp med sökväg `/solinteg` och dess API:er,
inklusive `PUT /solinteg/api/ev/settings`.
Kompletterar [EVCC-laddläges-ADR](adr-2026-11-05-ev-web-live-mode-selection.md).

## Beslut och betrodda gränser

Användaren kräver **GitHub OAuth2 för åtkomst utifrån**, men ingen
extra autentisering inne i hemnätet. Tidigare separat lösenordsfält
och Bearer-token för EV-laddläget är onödiga och har tagits bort.

- **Publik HTTPS:** Kubernetes Traefik `IngressRoute` för
  `Host(ermhawi.com)` har två rutter, `PathPrefix(/solinteg)`
  och `PathPrefix(/_next)`. **Båda** använder
  `oauth2-chain`, med `oauth2-forward-auth` via den
  befintliga OAuth2-proxyn (konfigurerad för GitHub).
  Oautentiserad publik GET och PUT gav HTTP **401** efter
  driftsättningen. Vi har **inte** testat en verkligt
  inloggad GitHub-session i denna kontroll; begränsningen
  verifieras med middleware-konfiguration och anonyma anrop.
- **Intern åtkomst:** loopback, RFC1918 LAN/VLAN,
  Kubernetes/containers samt Tailscale CGNAT betraktas
  uttryckligen som betrodda. Direkta anrop till den
  lokala Next-servern på port `3007` behöver **ingen**
  ytterligare inloggning. Detta innebär att en komprometterad
  intern klient kan anropa API:erna; det är en accepterad
  konsekvens av den definierade tillitsgränsen.
- **Spärr mot extern direktåtkomst:** en separat nftables-tabell
  `inet solinteg_web_internal` med två regler på **enbart**
  TCP/3007 nekar IPv4- och IPv6-källor utanför ovannämnda
  nät. All annan lokal TCP-trafik, Modbus, HA och brokern
  är oförändrade. Någon generell ändring av UFW,
  kube-proxy eller K3s-brandväggen gjordes inte.
  Regeluppsättningen är validerad med `nft -c`.
- **Beständig ordning:** `solinteg-web-internal-only.service`
  (oneshot, enabled/active) installerar/återläser den
  avgränsade tabellen. `solinteg-web.service` har
  `Requires=` och `After=` för nätgrinden. Ett fel vid
  installation av brandväggen förhindrar webbstart.
  Regeln tas **inte** automatiskt bort när tjänsten
  stoppas, så stopp öppnar inte porten.
- **Mutationer:** `EV_SETTINGS_WRITES_ENABLED=1` är
  endast en *funktionsflagga*, inte en autentiseringsnyckel.
  Webben skickar vanlig `application/json` och
  `credentials=same-origin`. API:t avvisar
  `Sec-Fetch-Site: cross-site` och mismatched/ogiltiga
  `Origin`-värden med HTTP **403**, för att begränsa
  CSRF från externa webbsidor. Skript utan `Origin` kan
  fortfarande användas på betrodda interna nät.
- **EVCC-säkerhet:** Servern validerar fortsatt datatyper,
  pristak, svensk avresetid, inställningsrevision,
  kabel/6 A-/pausägarstatus och atomiskt sparande.
  En ändring av önskat laddläge ger **ingen direkt FC10**.
  Reglerna mot att återuppta en tidigare prispaus
  är oförändrade. Andra API:er ändrades inte.

## Filer och installation

Källor:
`app/api/ev/settings/route.ts`,
`app/components/EvChargingPlanner.tsx`, `lib/ev-state.ts`,
`lib/__tests__/ev-settings-route.test.ts`,
`lib/__tests__/ev-settings.test.ts`,
`deploy/solinteg-web-internal-only.nft`,
`deploy/solinteg-web-internal-only.service`,
`deploy/solinteg-web-internal-only.conf`.

Privat Dell-installation:
- `/etc/solinteg/ev-web-settings.env`: mode 0600,
  nu **utan** `EV_SHADOW_SETTINGS_WRITE_TOKEN`.
  `EV_SETTINGS_WRITES_ENABLED=1` tillåter inställningsändring.
  Den tidigare separat genererade token återfinns
  **endast i skyddad återställningskopia** och är
  inte längre någon giltig behörighet.
- `/etc/solinteg/solinteg-web-internal-only.nft`
- `/etc/systemd/system/solinteg-web-internal-only.service`
- `/etc/systemd/system/solinteg-web.service.d/60-internal-only.conf`

Den tidigare Next.js-builden och de tidigare enskilda
webbkällorna är säkerhetskopierade. Den nya bundlen
byggdes i en **isolerad katalog från installerad bas**
och bara tre webb-/API-källfiler ersattes. Övriga
Solinteg-sidor och API:er byggdes oförändrade.

## Observerad funktion efter ändringen

| Prov | Resultat |
| --- | --- |
| `GET http://127.0.0.1:3007/solinteg/api/ev` | HTTP 200 |
| `GET http://LAN:3007/solinteg/api/ev` | HTTP 200 |
| `PUT` internt, **utan nyckel**, med **identiska** inställningar | HTTP 200, `deviceWrites=0` |
| `PUT` internt med extern `Origin` och `Sec-Fetch-Site: cross-site` | HTTP 403, ingen ändring |
| `GET https://ermhawi.com/solinteg/ev` utan OAuth-session | HTTP 401 |
| `GET https://ermhawi.com/solinteg/api/ev` utan OAuth-session | HTTP 401 |
| `PUT https://ermhawi.com/solinteg/api/ev/settings` utan OAuth-session | HTTP 401 |
| `nft list table inet solinteg_web_internal` | IPv4- och IPv6-spärr för TCP/3007 |
| `systemctl reload solinteg-web-internal-only.service` | Godkänt, två kvarstående drop-regler |

Det interna PUT-provet skickade **oförändrat**
`deadline/07:00`. Inställningsfilens bytes var
identiska före/efter. **Ingen ny EVCC-skrivning**
utlöstes av detta arbete.

## Begränsningar, övervakning och rollback

Tillitsgränsen bygger på nätets källadress.
En router eller mellanliggande proxy som gör SNAT
och får externa klienter att se ut som betrodda
LAN-källor kan kringgå just detta källadressfilter.
**Router-/port-forwardingkonfigurationen har inte
oberoende reviderats i denna ändring.**
Port `3007` ska inte publiceras med separat
NAT, NodePort eller annan ingress utanför den
OAuth2-skyddade Traefik-rutten.

Kontroller:
```sh
sudo nft list table inet solinteg_web_internal
systemctl status solinteg-web-internal-only.service --no-pager
systemctl status solinteg-web.service --no-pager
kubectl -n solinteg get ingressroute solinteg -o yaml
```

För att **tillfälligt stänga inställningsskrivning**
utan att påverka externa OAuth-/brandvägsskydd:
sätt `EV_SETTINGS_WRITES_ENABLED=0` i den privata
webbmiljön och starta om bara `solinteg-web.service`.

Privat rollback:
`/var/lib/solinteg-evshadow-rollback/ev-web-oauth-boundary-20261105/`
(mode 0700), manifest, originalkonfiguration,
ursprungliga webbkällor och tidigare `.next`.
Lämna **nft-grinden aktiv** vid återställning av
webbkoden, så att extern direktåtkomst fortsatt
är spärrad. Om grinden måste tas bort fordras
först separat verifierad åtkomstkontroll.
Behåll kopior för krypterat USB-arkiv och
verifierat återställningsprov enligt den
befintliga arkivpolicyn. Ingen automatisk
destruktiv städning införs.
