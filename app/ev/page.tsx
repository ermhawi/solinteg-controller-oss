import { connection } from 'next/server';
import AppShell from '@/app/components/AppShell';
import EvChargingPlanner from '@/app/components/EvChargingPlanner';
import { readEvOverview } from '@/lib/ev-state';

export default async function EvChargingPage() {
  // Snapshot files and shadow status are request-time, not build-time data.
  await connection();
  const overview = await readEvOverview();
  return (
    <AppShell>
      <EvChargingPlanner initialOverview={overview} />
    </AppShell>
  );
}
