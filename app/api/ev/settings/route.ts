import { EvSettingsConflict, EvSettingsUnsafe, evSettingsLiveConfigured,
  evSettingsWriteEnabled, saveEvSettings } from '@/lib/ev-state';
import { parseEvSettings } from '@/lib/ev-settings';

// The public /solinteg route and its APIs require GitHub OAuth2 at Traefik.
// Direct access is trusted only from the internal network. Reject browser
// cross-origin mutations; never trust a forwarded identity header as auth.
function sameOrigin(request: Request): boolean {
  if (request.headers.get('sec-fetch-site') === 'cross-site') return false;
  const origin = request.headers.get('origin');
  if (!origin) return true; // Internal scripts/curl without browser Origin.
  try {
    const source = new URL(origin);
    const host = request.headers.get('x-forwarded-host') ||
      request.headers.get('host') || new URL(request.url).host;
    return (source.protocol === 'http:' || source.protocol === 'https:') &&
      source.host === host;
  } catch {
    return false;
  }
}

export async function PUT(request: Request) {
  if (!evSettingsWriteEnabled()) {
    return Response.json(
      { error: 'Skrivning av laddinställningar är inte aktiverad' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  if (!sameOrigin(request)) {
    return Response.json(
      { error: 'Laddinställningar kan bara ändras från Solintegs egen webbplats' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  if (!request.headers.get('content-type')?.startsWith('application/json')) {
    return Response.json({ error: 'JSON krävs' }, { status: 415 });
  }
  const declared = Number(request.headers.get('content-length') ?? '0');
  if (!Number.isFinite(declared) || declared > 2048) {
    return Response.json({ error: 'För stora inställningar' }, { status: 413 });
  }
  let payload: unknown;
  try {
    const body = await request.text();
    if (body.length > 2048) {
      return Response.json({ error: 'För stora inställningar' }, { status: 413 });
    }
    payload = JSON.parse(body);
  } catch {
    return Response.json({ error: 'Ogiltig JSON' }, { status: 400 });
  }
  const settings = parseEvSettings(payload);
  if (!settings) {
    return Response.json({ error: 'Ogiltigt laddläge, klockslag eller prisgräns' },
      { status: 400 });
  }
  let saved;
  try {
    saved = await saveEvSettings(settings);
  } catch (error) {
    if (error instanceof EvSettingsConflict || error instanceof EvSettingsUnsafe) {
      return Response.json({ error: error.message },
        { status: 409, headers: { 'Cache-Control': 'no-store' } });
    }
    return Response.json(
      { error: 'Det gick inte att spara inställningar på servern' },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  return Response.json({
    settings: saved,
    mode: evSettingsLiveConfigured() ? 'live' : 'shadow',
    deviceWrites: 0,
  }, { headers: { 'Cache-Control': 'no-store' } });
}
