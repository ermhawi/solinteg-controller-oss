/** Distinguish a failed browser/API request from missing inverter telemetry. */
export type InverterApiStatus =
  | 'live'
  | 'inverter-offline'
  | 'auth-required'
  | 'service-error'
  | 'network-error';

export function classifyInverterResponse(res: Response): InverterApiStatus {
  // OAuth proxy can return 401/403, or redirect to a login page.
  if (res.status === 401 || res.status === 403 || res.type === 'opaqueredirect') return 'auth-required';
  if (res.status === 503) return 'inverter-offline';
  if (!res.ok) return 'service-error';
  // An expired session may return HTML (login page) with HTTP 200.
  if (!res.headers.get('content-type')?.includes('application/json')) return 'auth-required';
  return 'live';
}

export const INVERTER_STATUS_TEXT: Record<InverterApiStatus, string> = {
  live: 'Live · uppdaterad nyss',
  'inverter-offline': 'Ingen aktuell data från växelriktaren',
  'auth-required': 'Inloggningen har gått ut · ladda om sidan',
  'service-error': 'Fel i Solinteg-tjänsten',
  'network-error': 'Nätverksfel mot Solinteg-tjänsten',
};
