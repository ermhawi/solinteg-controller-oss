// Base path for client-side requests to our own API routes. Empty unless NEXT_BASE_PATH was set
// at build time (see next.config.ts, which surfaces it to the client as NEXT_PUBLIC_BASE_PATH).
// Next.js prefixes basePath onto <Link>, router navigation and next/image automatically, but NOT
// raw fetch() — so a client fetch('/api/…') escapes the sub-path mount (e.g. it hits
// ermhawi.com/api/… instead of ermhawi.com/solinteg/api/…, which is not routed to this app).
export const BASE_PATH = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

/** Prefix an app-absolute path (e.g. '/api/inverter') with the deployment base path. */
export function apiPath(path: string): string {
  return `${BASE_PATH}${path}`;
}
