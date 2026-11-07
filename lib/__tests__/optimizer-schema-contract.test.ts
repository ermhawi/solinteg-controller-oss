import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { DatabaseSync } from 'node:sqlite';

vi.mock('../telemetry/core', () => ({ getDb: vi.fn() }));
import { getDb } from '../telemetry/core';
import { optimizerRunsSchemaCompatible } from '../telemetry/dispatch';

let handle: DatabaseSync;
beforeEach(() => {
  handle = new DatabaseSync(':memory:');
  vi.mocked(getDb).mockReturnValue(handle);
});
afterEach(() => {
  handle.close();
  vi.mocked(getDb).mockReset();
});

const VALID = `CREATE TABLE optimizer_runs (
  id INTEGER PRIMARY KEY,
  logged_at TEXT NOT NULL,
  price_date TEXT NOT NULL,
  has_tomorrow INTEGER NOT NULL,
  start_soc_kwh REAL NOT NULL,
  inputs_json TEXT NOT NULL,
  dispatch_json TEXT NOT NULL
)`;

describe('O06 SQLite publisher schema metadata gate', () => {
  it('recognizes canonical required fields without reading any rows', () => {
    handle.exec(VALID);
    expect(optimizerRunsSchemaCompatible()).toBe(true);
    handle.exec("INSERT INTO optimizer_runs VALUES (1, '2026-11-07Z', '2026-11-07', 1, 6, '[]', '[]')");
    expect(optimizerRunsSchemaCompatible()).toBe(true);
  });

  it('rejects a missing, renamed or weakened schema', () => {
    expect(optimizerRunsSchemaCompatible()).toBe(false);
    handle.exec(VALID.replace('dispatch_json TEXT NOT NULL', 'dispatch_blob TEXT NOT NULL'));
    expect(optimizerRunsSchemaCompatible()).toBe(false);
  });

  it('rejects missing not-null constraints even when all fields exist', () => {
    handle.exec(VALID.replace('inputs_json TEXT NOT NULL', 'inputs_json TEXT'));
    expect(optimizerRunsSchemaCompatible()).toBe(false);
  });

  it('denies publication when DB is off or connection fails', () => {
    vi.mocked(getDb).mockReturnValue(null);
    expect(optimizerRunsSchemaCompatible()).toBe(false);
    vi.mocked(getDb).mockImplementation(() => { throw new Error('db down'); });
    expect(optimizerRunsSchemaCompatible()).toBe(false);
  });
});
