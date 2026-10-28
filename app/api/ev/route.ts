import { readEvOverview } from '@/lib/ev-state';

export async function GET() {
  const result = await readEvOverview();
  return Response.json(result, {
    headers: { 'Cache-Control': 'no-store, max-age=0' },
  });
}
