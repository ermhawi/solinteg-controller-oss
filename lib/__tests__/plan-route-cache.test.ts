import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

describe('GET /api/plan', () => {
  it('waits for the request before reading SQLite (never prerenders the saved plan)', () => {
    const source = readFileSync(fileURLToPath(new URL('../../app/api/plan/route.ts', import.meta.url)), 'utf-8');
    expect(source).toMatch(/await connection\(\);[\s\S]*readLatestOptimizerRun\(\)/);
  });
});
