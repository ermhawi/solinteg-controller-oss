import { describe, expect, it } from 'vitest';
import { classifyInverterResponse } from '../inverter-api-status';

describe('classifyInverterResponse', () => {
  it('recognizes valid live data responses', () => {
    expect(classifyInverterResponse(Response.json({ soc_pct: 80 }))).toBe('live');
  });

  it.each([401, 403])('treats %i as expired authentication, not an inverter outage', (code) => {
    expect(classifyInverterResponse(new Response('', { status: code }))).toBe('auth-required');
  });

  it('recognizes redirects to login and HTML login responses', () => {
    const redirected = { status: 0, type: 'opaqueredirect' } as Response;
    expect(classifyInverterResponse(redirected)).toBe('auth-required');
    expect(classifyInverterResponse(new Response('<html>Login</html>', {
      headers: { 'content-type': 'text/html' },
    }))).toBe('auth-required');
  });

  it('reserves inverter-offline for unavailable live telemetry', () => {
    expect(classifyInverterResponse(Response.json({}, { status: 503 }))).toBe('inverter-offline');
    expect(classifyInverterResponse(Response.json({}, { status: 500 }))).toBe('service-error');
  });
});
