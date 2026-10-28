import Link from 'next/link';
import { stockholmDateOf } from '@/lib/economics';
import AutoRefresh from '@/app/components/AutoRefresh';
import LiveStatus from '@/app/components/LiveStatus';

export default function AppShell({ children }: { children: React.ReactNode }) {
  const updatedAt = new Date();
  const today = stockholmDateOf(updatedAt.toISOString());
  const updatedTime = updatedAt.toLocaleTimeString('sv-SE', { timeZone: 'Europe/Stockholm' });

  return (
    <div
      className="mx-auto flex max-w-[1720px] flex-col gap-[18px] px-4 pt-[16px] pb-[30px] min-[700px]:pt-[26px] min-[480px]:px-5 min-[768px]:px-[30px]"
      style={{ fontFamily: 'var(--font-body)' }}
    >
      {/* Keeps the server props (chart/plan/SoC overlay) from freezing at load time —
          the "Live" header below is only honest because of this. */}
      <AutoRefresh renderedAt={updatedAt.toISOString()} />
      {/* Slimmed header (title text dropped) — just a small house-icon mark next to the
          live-status line, no more two-line title/subtitle stack. */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-2.5">
          <div
            className="flex h-7 w-7 items-center justify-center rounded-lg"
            style={{ background: 'var(--node-bg)', boxShadow: '0 10px 22px -12px rgba(60,80,120,.35)' }}
          >
            <svg viewBox="0 0 100 100" style={{ width: 16, height: 16, color: 'var(--logo-color)' }}>
              <polygon points="50,14 87,45 13,45" fill="currentColor" />
              <rect x="20" y="43" width="60" height="45" rx="8" fill="currentColor" />
              <rect x="42" y="60" width="16" height="28" rx="3" fill="var(--node-bg)" />
            </svg>
          </div>
          <LiveStatus today={today} updatedTime={updatedTime} />
        </div>
        <Link href="/ev" className="rounded-xl border px-3 py-2 text-[13px] font-semibold"
          style={{ borderColor: 'var(--card-border)', color: 'var(--text)' }}>
          Volvo · Laddplanering
        </Link>
      </div>

      {children}
    </div>
  );
}
