import { connection } from 'next/server';
import { fetchPricesUncached } from '@/lib/prices';

export async function GET() {
  try {
    await connection();
    // The forecast extension can change several times before the next day-ahead
    // auction. Do not reuse the midnight cache entry here: it made the chart show
    // an old forecast even after PlaneraElen had published a newer one.
    const data = await fetchPricesUncached();
    return Response.json(data, {
      headers: { 'Cache-Control': 'private, no-store' },
    });
  } catch {
    return Response.json({ error: 'Failed to fetch prices' }, { status: 502 });
  }
}
