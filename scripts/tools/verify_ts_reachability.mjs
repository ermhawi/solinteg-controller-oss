/** Source-only regression for A05. Uses node:test, not Vite transform.
 * Never loads app code or invokes any physical entrypoint.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  collectSourceFacts, collectProductionFiles, isNextEntrypoint, resolveImport, analyze,
} from './ts_reachability_audit.mjs';

test('recognize Next entrypoints but no normal component as a root', () => {
  assert.equal(isNextEntrypoint('app/page.tsx'), true);
  assert.equal(isNextEntrypoint('app/api/control-evidence/route.ts'), true);
  assert.equal(isNextEntrypoint('app/ev/page.tsx'), true);
  assert.equal(isNextEntrypoint('app/components/PriceChart.tsx'), false);
  assert.equal(isNextEntrypoint('lib/plan.ts'), false);
});

test('AST imports, computed import, literal condition, env key and models', () => {
  const facts = collectSourceFacts([
    "import x from '@/lib/x';",
    "export { f } from './f';",
    "const later = import('@/lib/later');",
    'const dynamic = import(expression);',
    "if (false) { throw new Error('never'); }",
    "const gate = process.env.SOLINTEG_HOLD_ENABLED === '1';",
    'interface BatteryForecast { soc: number }',
    'type EnergyPlan = BatteryForecast;',
  ].join('\n'));
  assert.deepEqual(facts.imports, ['@/lib/x', './f', '@/lib/later']);
  assert.deepEqual(facts.dynamic, [4]);
  assert.deepEqual(facts.flags, [{ name: 'SOLINTEG_HOLD_ENABLED', line: 6 }]);
  assert.deepEqual(facts.unreachable, [{ kind: 'constant-condition', line: 5, value: false }]);
  assert.deepEqual(facts.models, [
    { name: 'BatteryForecast', line: 7 }, { name: 'EnergyPlan', line: 8 },
  ]);
});

test('resolve aliases, local relative imports, but not node dependencies', () => {
  const known = new Set(['lib/constants.ts', 'app/components/PriceChart.tsx', 'lib/price/index.ts']);
  assert.equal(resolveImport('app/page.tsx', '@/lib/constants', known), 'lib/constants.ts');
  assert.equal(resolveImport('app/page.tsx', './components/PriceChart', known),
               'app/components/PriceChart.tsx');
  assert.equal(resolveImport('lib/a.ts', './price', known), 'lib/price/index.ts');
  assert.equal(resolveImport('app/page.tsx', 'next/server', known), null);
});

test('every production module is reached and no deletion follows automatically', () => {
  const files = collectProductionFiles();
  const report = analyze();
  assert.equal(report.schema, 'solinteg-ts-static-review-v1');
  assert.equal(report.source_modules, files.length);
  assert.ok(files.length >= 83);
  assert.ok(report.entrypoints.length >= 10);
  assert.equal(report.reachable_modules.length, files.length);
  assert.deepEqual(report.unreachable_modules, []);
  assert.deepEqual(report.computed_dynamic_imports, []);
  assert.deepEqual(report.explicit_constant_conditions, []);
});

test('feature flags and similar model names remain evidence only', () => {
  const report = analyze();
  assert.ok(Object.keys(report.feature_flags).length >= 10);
  assert.ok(report.audit_limitations.join(' ').includes('not proof'));
  assert.ok(report.unreachable_modules.every(row => row.deletion_proposed === false));
  assert.ok(report.candidate_model_collisions.every(item => item.entries.length > 1));
});
