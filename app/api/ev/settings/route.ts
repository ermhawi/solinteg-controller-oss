import { createHash, timingSafeEqual } from 'node:crypto';
import { evSettingsWriteEnabled, readEvOverview, saveEvSettings } from '@/lib/ev-state';
import { parseEvSettings } from '@/lib/ev-settings';

function authorized(request: Request): boolean {
  if (!evSettingsWriteEnabled()) return false;
  const expected = process.env.EV_SHADOW_SETTINGS_WRITE_TOKEN ?? '';
  const authorization = request.headers.get('authorization');
  if (!authorization?.startsWith('Bearer ')) return false;
  const provided = authorization.slice('Bearer '.length);
  // Hash before comparison so timingSafeEqual receives equal-size buffers.
  const hash = (text: string) => createHash('sha256').update(text).digest();
  return timingSafeEqual(hash(expected), hash(provided));
}

export async function PUT(request: Request) {
  if (!evSettingsWriteEnabled()) {
    return Response.json(
      { error: 'Skrivning av skugginställningar är inte aktiverad' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  if (!authorized(request)) {
    return Response.json(
      { error: 'Behörighet saknas för att spara skugginställningar' },
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
  try {
    await saveEvSettings(settings);
  } catch {
    return Response.json(
      { error: 'Det gick inte att spara inställningar på servern' },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  const overview = await readEvOverview();
  return Response.json({
    settings: overview.settings,
    mode: 'shadow',
    deviceWrites: 0,
  }, { headers: { 'Cache-Control': 'no-store' } });
}
