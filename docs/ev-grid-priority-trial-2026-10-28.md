# 2026-10-28 — temporärt fysiskt L3/EV-nätprioriteringsprov

## Syfte och användarens tillstånd

Användaren godkände efter tidigare read-only analys **en tidsbegränsad
och övervakad inverter-batterireglering**: låt XC40:n ladda från nätet,
och låt husbatteriet försörja annan huslast där det faktiskt är möjligt.
**Ingen** förändring av DEFA/EVCC 6 A, Volvos 80 %, EVCC FC10, bilen,
Solintegs solplanerare eller batteriets BMS-/SoC-skydd har godkänts.
Detta är ett kontrollerat prov, **inte** generell permanent utrullning.

## Varför en enkel EV-1,4 kW-subtraktion är fel

Solinteg läser `grid_unbalanced_output_raw=0`: växelriktaren matar
då ungefär balanserat över husets tre faser. Om den levererar
`inverter_ac_w`, blir dess L3-andel ungefär en tredjedel.
Enfasladdande XC40:n sitter på **L3** och EVCC Ict2 rapporterar
ungefär **5,9–6,1 A**, alltså **1,35–1,40 kW vid antagna 230 V**.

En rimlig uppskattning är därför:

```text
house_L3_W          ~= -meter_l3_w + inverter_ac_w/3
EV_L3_W             ~= Ict2_A * 230 V
non_EV_L3_W         ~= house_L3_W - EV_L3_W
allowed_inverter_L3 ~= max(0, non_EV_L3_W - 180 W safety margin)
battery_DC_target   <= 3 * allowed_inverter_L3 / 0.95
```

Med från 21:40–21:59 observerad `non_EV_L3` ≈ 145–150 W
skulle även flera hundra W batteriuttag lägga onödig el på L3.
Minsta verifierat användbara skyddade EMS-discharge-setpoint är
**50 W**, som i drift ger uppmätt **180–260 W** batteriurladdning.
Det sista är inverterbeteende, inte ett korrekt märkvärde för
50 W utmatning. Om annan L3-huslast ökar kan policyn tillåta
**mer** batteriuttag, men aldrig mer än den ursprungliga planen.

## Kvalitets- och säkerhetsvillkor

Pure read-only policyn `scripts/services/ev_grid_priority_guard.py`
kräver ALLT nedan innan den ens får rekommendera lägre setpoint:

- Rotägd, tidsbegränsad fil `/opt/solinteg/ev-grid-priority-trial.json`,
  schema 1, `mode=active`, max 900 s, giltigt UTC-intervall.
  Saknas/utgått -> **ingen påverkan på det gamla dispatch-beslutet**.
- Invertervärden färska inom 60 s, EVCC inom 65 s; båda korrekt
  tidsstämplade. `grid_unbalanced_output_raw` exakt heltalet 0,
  alltså verifierat balanserad inverterinställning 50006 (pollerns
  färska Modbus-avläsning), L1+L2+L3 stämmer mot grid-mätaren,
  inverter AC minus grid stämmer mot total huslast.
- Verifierad EVCC FC03 `C2`, `charging_detected=true`,
  `read_ok=true`, `writes=0`, `physical_control_eligible=false`,
  PWM pilot/config **100 = 6 A**, strömmen på **Ict2** mellan
  5,0–6,5 A; Ict1/Ict3 endast brus upp till 0,5 A.
  Ingen inferens om 0 A vid timeout.
- Batteri SoC 25–95 %, temperatur **10–45 °C** och PV ≤100 W;
  BMS skydd, fysiskt SoC-golv, inverterbegränsning och befintlig
  L3 charge guard förblir orörda. Ingen extra fysisk laddström
  eller batteriladdning tillåts av den nya policyn.
- Efter rekommenderad minskning måste förväntad L3-import
  ligga under **3 200 W** (≈14 A vid antagna 230 V, långt under
  20 A-huvudsäkringens märkström). Total förväntad import ska
  ligga under **6 500 W**, utöver ordinarie site import limit
  **10 500 W**. Detta är ekonomiska/testmässiga gränser, **inte
  certifierat RMS-baserat säkringsskydd**.
- Ändringen gäller ENDAST planens `action=discharge`, aldrig
  `charge`, `idle`, aktiv SoC-recovery eller safety.
  `expected_soc_kwh=None` sätts för legitim justering av
  SoC-bana (undvik osäker divergence->Auto-batteriurladdning).
- Fel i provunderlaget ger `unavailable`, inte en falsk
  nollampsobservation. **Oberoende watchdog** ska då återställa
  den ursprungliga exekveraren.

## Deployment — surgiskt på äldre körande exekverare

**Viktig driftavvikelse F-11:** på Dell körde `solinteg-dispatch.service`
en äldre `/opt/solinteg/app/scripts/services/dispatch_loop.py`
från 2026-10-26, medan Git HEAD har många ytterligare ändringar.
Vi har **inte** deployat hela HEAD till den ARMED tjänsten.
I stället gjordes en minimal additiv import/hook före
`guard_l3_charge` i den redan använda filen, och originalet
säkerhetskopierades med root **0400** till
`/opt/solinteg/ev-grid-priority-dispatch-before.py`.
Enheten använder fortfarande alla sina ordinarie batterigränser
och optimerar fortfarande huslasten.

- Först rent test av policymodulen mot tidsstämplade verkliga
  avläsningar; 6 A och 2,3–5,7 kW ursprunglig urladdning gav
  rekommenderade **50 W** med L3 under konservativa gränsen.
- `solinteg-ev-grid-trial-watchdog.timer` installerad och aktiv
  **före** något aktivt styrtest, oberoende root-timer var 15 s.
  Dess kontrollkod har ingen direkt inverter- eller EVCC-klient.
- Exekveraren startades om med `mode=shadow`, och loggade
  förslag utan att förändra redan planerad fysisk urladdning.
- **21:59:55 CET** aktiverades tidsbegränsad root-gate till
  **22:09:55 CET** (2026-10-28). Watchdog kontrollerade
  inkommande värden och loggade `active_monitor_ok`.
- **22:00:17 CET:** optimeraren ville `discharge 2500 W`;
  skyddet ändrade det till **50 W**, `outcome=applied`,
  `ev_grid_trial_ict2_a=5.9`,
  `ev_grid_trial_ev_w_est=1357`,
  `ev_grid_trial_non_ev_l3_w_est=148`,
  `ev_grid_trial_predicted_l3_import_w=1490`.
- **22:00:36 CET:** faktisk inverteravläsning
  batteriurladdning **180 W**, nätimport **2 194 W**,
  L3-import **1 427 W**, huslast **2 417 W**, PV 0,
  husbatteri SoC **64 %**, temperatur **15 °C**.
  Bilen laddade fortfarande och den oberoende
  watchdoggen rapporterade `active_monitor_ok`.

## Automatisk avbrytning och återställning

`scripts/services/ev_grid_trial_watchdog.py` kör som egen
systemd oneshot timer (root, separat från dispatch-processen).
Den bevakar gate-deadline, tjänstens status, processens
guard-hook, färska EVCC/Ict2/C2, inverter-SoC/temperatur/PV,
CT-balans och fasgränser. Efter en kort 100-sekunders
tillämpningsgrace ska faktisk batteriurladdning inte ligga
långvarigt över det nya föreslagna setpointet +650 W,
med en konservativ miniminivå på **850 W**.

Om **något** villkor fallerar eller klockan når slutet:
1. Avaktivera aktiv gate först (atomisk fil-rename).
2. Stoppa exekveraren (dess ordinarie shutdown går tillbaka
   till auto med befintliga invertergränser).
3. Återställ säkerhetskopierade ursprungliga `dispatch_loop.py`
   atomiskt med korrekta rättigheter.
4. Starta den gamla dispatch-tjänsten igen och verifiera
   aktiv status. Skriver anonym rollback-statusfil.
5. Inga EVCC-/Volvo-/batteri-säkerhetsregister ändras
   direkt av watchdoggen. Vid restart-fel rapporteras
   `rollback_failed_dispatch_may_be_stopped`.

Skriptet körs fortsatt enligt 15-s timer efter trialen men
`no_active_trial` ger ingen återstart och ingen styrning.
Efter verifierad rollback kan man inaktivera **endast
watchdog-timern**, men man ska behålla övriga EVCC/
Home Assistant-insamlare och 6 A-piloten.

## Faktiskt provslut och återställning (verifierat)

Efter mer än fem minuters stabilt resultat avslutades det aktiva
provet kontrollerat i förtid för att **verifiera exakt samma
oberoende deadline-återställning** som annars skulle ha skett
senast 22:09:55. Klockan **22:05:05 CET** sattes
den root-ägda gate-filens `expires_utc` bakåt en sekund.
Den separata systemd-watchdoggen aktiverades och
**22:05:06** registrerade den:

```json
{
  "state": "restored_original_dispatch",
  "reason": "scheduled trial deadline",
  "rollback_ok": true
}
```

Verifierat med faktisk bytejämförelse: den återställda
`/opt/solinteg/app/scripts/services/dispatch_loop.py`
är **identisk** med 0400-backupen, importen/hooken
finns inte längre, aktiv gate-fil saknas och
`solinteg-dispatch.service` startade igen **22:05:12**.
Separat watchdog-timer inaktiverades efter lyckad
restore (`inactive/disabled`), så ingen kvarvarande
15-sekunderspoll kör i onödan. De ursprungliga
EVCC-observatörs-/HA-insamlarna fortsätter, och DEFA
rapporterar fortfarande C2 och **6 A PWM**.

**Viktigt återfall direkt efter rollback:** ordinarie äldre
dispatcher beordrade åter **2 600 W** urladdning vid
**22:05:25**. Färsk fysisk inverteravläsning
**22:05:37** visade husbatteri **2 581 W**
urladdning, total nätutväxling **+28 W export**,
L3-import **712 W**, husbatteri-SoC **63 %**,
PV **0 W**, medan XC40 laddade runt 6 A.
Det visar tydligt att det nya skyddet fungerade
under provet men att det tidigare problemet
**återkommer utan permanent batteristyrningsintegration**.

**Nästa beslut:** separat godkännande krävs för en
varaktig, övervakad batteri-L3-policy i den
normala exekveraren. För det bör deploy-driften
standardiseras mot källrepo först (F-11); testad
funktion `ev_grid_priority_guard.py` och
watchdog finns nu versionshanterade, men
DEN temporära fysiska ändringen är avaktiverad.
Inga privata .env, hårdvarukoder eller fakturor
får skickas till origin utan granskning.

**Tester:** 16 policyscenarier med historiska avläsningar,
9 oberoende rollback-/watchdogfall, plus full regressionssvit
(**782 Python-tester OK**). Ingen remote-push av de
oproportionerligt många äldre lokala git-commits utan
granskning av potentiellt privata konfigurationer.

**Begränsningar:** fasström är beräknad från fas-effekt
och 230 V antagande, inte en oberoende, certifierad
RMS-strömsäkring. Allokering av elektronflödet kan
inte bevisas direkt, men fallande faktisk total
batteriurladdning och L3-import nära bilens
1,35–1,40 kW är stark praktisk indikation.
En **permanent** införing behöver separat
acceptans, bättre BMS- och RMS-kontroll,
full GitOps-version av exekveraren samt fler
prov med variationer i huslasten.
