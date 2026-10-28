'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { apiPath } from '@/lib/base-path';
import type { EvChargeMode, EvOverview, EvSettings } from '@/lib/ev-settings';

function timeInSweden(iso: string | null | undefined): string {
  if (!iso) return '–';
  const date = new Date(iso);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString('sv-SE', { timeZone: 'Europe/Stockholm',
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : '–';
}

const muted: React.CSSProperties = { color: 'var(--text-muted)' };

function ModeChoice({ value, selected, title, description, onChange }: {
  value: EvChargeMode; selected: boolean; title: string; description: string;
  onChange: (next: EvChargeMode) => void;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-2xl border p-4 transition"
      style={{
        borderColor: selected ? 'var(--logo-color)' : 'var(--card-border)',
        background: selected ? 'var(--node-bg)' : 'var(--card-bg)',
      }}>
      <input type="radio" name="ev-mode" value={value} checked={selected}
        onChange={() => onChange(value)} className="mt-1 accent-emerald-500" />
      <span className="flex min-w-0 flex-col gap-1">
        <strong className="text-[15px]">{title}</strong>
        <span className="text-[13px] leading-relaxed" style={muted}>{description}</span>
      </span>
    </label>
  );
}

export default function EvChargingPlanner({ initialOverview }: {
  initialOverview: EvOverview;
}) {
  const [overview, setOverview] = useState(initialOverview);
  const [draft, setDraft] = useState<EvSettings>(initialOverview.settings);
  const [priceText, setPriceText] = useState(
    initialOverview.settings.max_buy_ore_per_kwh?.toString() ?? '',
  );
  const [secret, setSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(apiPath('/api/ev'), {
        cache: 'no-store', credentials: 'same-origin',
      });
      if (!response.ok) return;
      const result = await response.json() as EvOverview;
      setOverview(result);
    } catch {
      // Network failures are not evidence that old data is fresh.
    }
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => { void refresh(); }, 30_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function save() {
    setError(null);
    setNotice(null);
    if (!overview.settingsWriteEnabled) {
      setError('Servern tillåter ännu inte ändring av skugginställningar.');
      return;
    }
    if (!secret) {
      setError('Ange nyckeln för skugginställningarna för att spara.');
      return;
    }
    const candidate: EvSettings = {
      ...draft,
      max_buy_ore_per_kwh: priceText.trim() === '' ? null : Number(priceText),
    };
    if (candidate.max_buy_ore_per_kwh !== null &&
        (!Number.isFinite(candidate.max_buy_ore_per_kwh) ||
         candidate.max_buy_ore_per_kwh < -200 || candidate.max_buy_ore_per_kwh > 3000)) {
      setError('Prisgränsen måste vara mellan -200 och 3000 öre/kWh.');
      return;
    }
    setSaving(true);
    try {
      const response = await fetch(apiPath('/api/ev/settings'), {
        method: 'PUT',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + secret,
        },
        body: JSON.stringify(candidate),
      });
      const payload = await response.json() as {
        error?: string;
        settings?: EvSettings;
        deviceWrites?: number;
      };
      if (!response.ok) {
        setError(payload.error ?? 'Inställningarna kunde inte sparas.');
        return;
      }
      setDraft(payload.settings ?? candidate);
      setPriceText(candidate.max_buy_ore_per_kwh?.toString() ?? '');
      setNotice('Sparat för skuggläge. Ingen fysisk laddstyrning har aktiverats.');
      setSecret('');
      await refresh();
    } catch {
      setError('Kunde inte nå Solintegs inställnings-API.');
    } finally {
      setSaving(false);
    }
  }

  const actual = overview.statusFresh ? overview.status : null;
  const shadow = overview.shadowFresh ? overview.shadow : null;
  const candidateMode = draft.charge_mode;
  const unsaved = JSON.stringify(draft) !== JSON.stringify(overview.settings) ||
    priceText !== (overview.settings.max_buy_ore_per_kwh?.toString() ?? '');

  return (
    <main className="mx-auto w-full max-w-5xl space-y-5 pb-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-[12px] font-semibold uppercase tracking-widest" style={muted}>
            Volvo XC40 • Solinteg
          </p>
          <h1 className="mt-1 text-2xl font-bold" style={{ fontFamily: 'var(--font-heading)' }}>
            Smart laddplanering
          </h1>
          <p className="mt-2 max-w-2xl text-[14px]" style={muted}>
            Gemensamma elpriser för bilen och husbatteriet. Två olika laddmål,
            men fortfarande utan fysisk styrning.
          </p>
        </div>
        <Link href="/" className="rounded-xl border px-4 py-2 text-sm font-semibold"
          style={{ borderColor: 'var(--card-border)', color: 'var(--text)' }}>
          Till energiöversikten
        </Link>
      </div>

      <section className="card-surface p-4 md:p-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-bold">Laddläge</h2>
          <span className="rounded-full px-3 py-1 text-xs font-bold"
            style={{ background: 'var(--badge-bg)', color: 'var(--badge-color)' }}>
            SKUGGLÄGE · 0 skrivningar
          </span>
        </div>
        <p className="mt-2 text-sm" style={muted}>
          Sparat val: {overview.settings.charge_mode === 'deadline'
            ? 'Klar till avresa' : 'Ladda ekonomiskt'}
          {unsaved ? ' · Du förhandsvisar osparade ändringar' : ''}
        </p>
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
          <ModeChoice
            value="deadline" selected={candidateMode === 'deadline'}
            onChange={(charge_mode) => setDraft((current) => ({ ...current, charge_mode }))}
            title="Klar till avresa"
            description="Försök nå 80 % vid avresetiden. Ladda så billigt som möjligt, men använd även dyr el om det behövs för att hinna."
          />
          <ModeChoice
            value="economy" selected={candidateMode === 'economy'}
            onChange={(charge_mode) => setDraft((current) => ({ ...current, charge_mode }))}
            title="Ladda ekonomiskt"
            description="Ingen avresetid. Använd bara billiga timmar och lämpligt solöverskott; det får ta den tid det tar. Stanna vid 80 %."
          />
        </div>

        <div className="mt-5 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="ev-target" className="block text-sm font-semibold">Laddmål</label>
            <div id="ev-target" className="mt-2 rounded-xl border px-4 py-3 text-lg font-bold"
              style={{ borderColor: 'var(--card-border)' }}>80 %</div>
            <p className="mt-1 text-xs" style={muted}>Samma mål i båda lägena.</p>
          </div>
          {candidateMode === 'deadline' ? (
            <div>
              <label htmlFor="ev-departure" className="block text-sm font-semibold">
                Avresetid (svensk tid)
              </label>
              <input id="ev-departure" type="time" value={draft.departure_local_time}
                onChange={(event) => setDraft((current) => ({
                  ...current, departure_local_time: event.target.value,
                }))}
                className="mt-2 w-full rounded-xl border px-4 py-3 text-lg"
                style={{ background: 'var(--card-bg)', borderColor: 'var(--card-border)' }} />
              <p className="mt-1 text-xs" style={muted}>
                Nästa tillfälle vid detta klockslag, med svensk sommar-/vintertid.
              </p>
            </div>
          ) : (
            <div>
              <label htmlFor="ev-price" className="block text-sm font-semibold">
                Högsta totala köppris (öre/kWh)
              </label>
              <input id="ev-price" type="number" min="-200" max="3000" step="0.01"
                value={priceText} placeholder="Valfritt – välj billiga kvartarna"
                onChange={(event) => setPriceText(event.target.value)}
                className="mt-2 w-full rounded-xl border px-4 py-3 text-[15px]"
                style={{ background: 'var(--card-bg)', borderColor: 'var(--card-border)' }} />
              <p className="mt-1 text-xs" style={muted}>
                Tomt = de billigaste cirka 25 % av prognostimmarna,
                aldrig automatiskt alla timmar vid ett jämnt högt pris.
              </p>
            </div>
          )}
        </div>

        {overview.settingsWriteEnabled ? (
          <div className="mt-5 flex flex-wrap items-end gap-3">
            <div className="min-w-[180px] flex-1 sm:max-w-[330px]">
              <label htmlFor="ev-admin-key" className="block text-sm font-semibold">
                Nyckel för skugginställningar
              </label>
              <input id="ev-admin-key" type="password" value={secret} autoComplete="off"
                onChange={(event) => setSecret(event.target.value)}
                placeholder="Ange lokal administratörsnyckel"
                className="mt-2 w-full rounded-xl border px-4 py-3 text-sm"
                style={{ background: 'var(--card-bg)', borderColor: 'var(--card-border)' }} />
            </div>
            <button type="button" onClick={() => { void save(); }}
              disabled={saving || !secret}
              className="rounded-xl px-5 py-3 text-sm font-bold disabled:cursor-not-allowed disabled:opacity-50"
              style={{ background: 'var(--logo-color)', color: '#fff' }}>
              {saving ? 'Sparar…' : 'Spara skugginställningar'}
            </button>
          </div>
        ) : (
          <p className="mt-5 rounded-xl border p-3 text-[13px]"
            style={{ borderColor: 'var(--card-border)', background: 'var(--node-bg)' }}>
            Inställningarna är låsta på servern. Du kan förhandsvisa båda
            laddlägena här, men valet sparas inte förrän separat åtkomst för
            skugginställningar har konfigurerats.
          </p>
        )}
        {notice && <p className="mt-3 text-sm font-semibold" role="status"
          style={{ color: 'var(--logo-color)' }}>{notice}</p>}
        {(error || overview.settingsError) && (
          <p className="mt-3 text-sm" role="alert" style={{ color: 'var(--flow-red)' }}>
            {error || overview.settingsError}
          </p>
        )}
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <section className="card-surface p-4 md:p-5">
          <h2 className="text-lg font-bold">Volvo i Home Assistant</h2>
          {!actual ? (
            <p className="mt-3 text-sm" style={muted}>
              Ingen aktuell Volvo-observation från läsaren.
              {overview.status?.reason ? ' Orsak: ' + overview.status.reason : ''}
              {' '}Inga laddförslag grundas på ett gammalt API-svar.
            </p>
          ) : (
            <>
              <div className="mt-3 flex items-baseline gap-3">
                <strong className="text-3xl font-bold">
                  {actual.shadow_observation_usable && actual.battery_soc_pct !== null
                    ? String(actual.battery_soc_pct) + ' %' : '–'}
                </strong>
                <span className="text-sm" style={muted}>Laddmål 80 %</span>
              </div>
              <p className="mt-2 text-sm" style={muted}>
                {actual.shadow_observation_usable
                  ? (actual.connected ? 'Laddkabel ansluten' : 'Kabel ej ansluten')
                  : 'Observation inte verifierad'}
              </p>
              <p className="mt-2 text-[13px]" style={muted}>
                Volvo-API hämtat: {timeInSweden(actual.energy_api_received_at_utc)}
              </p>
              <p className="mt-1 text-[13px]" style={muted}>
                Batterivärdet senast ändrat: {timeInSweden(actual.battery_reported_at_utc)}
              </p>
              <p className="mt-2 text-sm" style={muted}>
                Kvar till 80 %: {actual.remaining_soc_pct === null
                  ? 'okänt' : String(actual.remaining_soc_pct) + ' procentenheter'}
              </p>
              {actual.shadow_estimate_ready && actual.remaining_ac_kwh !== null ? (
                <p className="mt-2 text-sm" style={muted}>
                  Illustrativt AC-behov: {actual.remaining_ac_kwh} kWh,
                  antaget {actual.assumed_usable_capacity_kwh} kWh användbar
                  kapacitet och {actual.assumed_ac_efficiency === null
                    ? 'okänd' : Math.round(actual.assumed_ac_efficiency * 100) + ' %'}{' '}
                  laddverkningsgrad.
                </p>
              ) : (
                <p className="mt-2 text-sm" style={muted}>
                  Ingen energiberäkning är aktiverad. Skuggsimulering kräver
                  uttryckligt angivna och markerat obekräftade antaganden
                  om kapacitet och laddförluster.
                </p>
              )}
              <p className="mt-3 text-[13px] font-semibold" style={muted}>
                {actual.shadow_observation_usable
                  ? 'Preliminär API-observation – inte verifierad BMS-mätning.'
                  : 'Ingen godkänd API-observation för skuggläge.'}
              </p>
              {actual.shadow_reason && <p className="mt-1 text-xs" style={muted}>
                {actual.shadow_reason}
              </p>}
              {!actual.shadow_observation_usable && actual.reason && (
                <p className="mt-2 text-xs" style={{ color: 'var(--flow-red)' }}>
                  {actual.reason}
                </p>
              )}
            </>
          )}
          <p className="mt-3 text-xs" style={muted}>
            Volvos senast ändrat-tid är inte samma sak som tidpunkten för en
            ny batterimätning. Fysisk laddstyrning är alltid spärrad här.
          </p>
        </section>

        <section className="card-surface p-4 md:p-5">
          <h2 className="text-lg font-bold">Simulerad laddplan</h2>
          {unsaved && (
            <p className="mt-2 text-[13px]" style={muted}>
              Simuleringen nedan gäller fortfarande det senast sparade laddläget.
              Ändringarna ovan är endast en förhandsvisning.
            </p>
          )}
          {shadow ? (
            <>
              <p className="mt-3 text-3xl font-bold">
                {shadow.charging_suggested_a === null
                  ? '–' : String(shadow.charging_suggested_a) + ' A'}
              </p>
              <p className="mt-2 text-sm" style={muted}>
                Endast förslag, inte utförande. Tillåten fysisk ström:
                {' '}{shadow.charging_permitted_a} A.
              </p>
              {shadow.source === 'ha_price_scenario_no_evcc' && (
                <p className="mt-2 text-[13px] font-semibold" style={muted}>
                  Skuggscenario från husets prisplan och Volvo-API. Bilens
                  verkliga laddström är inte mätt via EVCC.
                </p>
              )}
              {shadow.energy_estimate_unverified && (
                <p className="mt-2 text-[13px]" style={muted}>
                  Varning: kWh-behovet bygger på antaganden om bilens kapacitet
                  och laddförluster, inte på en kalibrerad mätning.
                </p>
              )}
              <p className="mt-2 text-sm" style={muted}>
                Köppris: {shadow.price_buy_ore_kwh === null
                  ? 'okänt' : String(shadow.price_buy_ore_kwh) + ' öre/kWh'}
              </p>
              {shadow.price_cheap_threshold_ore_kwh !== null &&
                <p className="mt-1 text-sm" style={muted}>
                  Ekonomilägets aktuella pristak:
                  {' '}{shadow.price_cheap_threshold_ore_kwh} öre/kWh
                </p>}
              <p className="mt-2 text-xs" style={muted}>
                {shadow.price_reason || 'Skuggbeslut saknar motivering'}
                {' · '}Mätt {timeInSweden(shadow.timestamp)}
              </p>
            </>
          ) : (
            <p className="mt-3 text-sm" style={muted}>
              Ingen färsk skuggsimulering. Den separata, skrivskyddade
              prisplaneraren måste vara aktiverad. Waveshare krävs inte
              för detta steg.
            </p>
          )}
          <p className="mt-3 text-xs font-semibold" style={{ color: 'var(--logo-color)' }}>
            Fysisk laddstyrning avstängd
          </p>
        </section>
      </div>
    </main>
  );
}
