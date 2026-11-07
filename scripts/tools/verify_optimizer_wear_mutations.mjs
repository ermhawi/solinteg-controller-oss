/** K05: execute optimizer wear-cost mutants in a sandbox, never alter source.
 *
 * We extract the source function using the TypeScript AST, transpile *only
 * that function*, and inject fixed independent numeric constants into node:vm.
 * This performs no plan publication, filesystem mutation, or inverter I/O.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import ts from 'typescript';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const file = path.join(repo, 'lib/optimizer.ts');
const ast = ts.createSourceFile(file, readFileSync(file, 'utf8'), ts.ScriptTarget.Latest, true);
const wear = ast.statements.find(n => ts.isFunctionDeclaration(n) && n.name?.text === 'wearOreFor');
if (!wear) throw new Error('optimizer wear source function not found');
const original = wear.getText(ast);

function evaluate(source) {
  const compiled = ts.transpileModule(source, {
    fileName: 'wear-cost-mutation.ts',
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None },
  });
  return vm.runInNewContext(compiled.outputText + '\nwearOreFor', {
    Math, BATTERY_WEAR_COST_ORE_PER_KWH: 4.2, ONE_WAY_EFF: 0.95,
  }, { timeout: 500 });
}

function independentContract(fn) {
  const approx = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9,
    'independent cost evidence rejected a mutated result');
  approx(fn(1, 1), 0); // unavoidably absorbed free solar has no wear price
  approx(fn(1, 0), 4.2 / 0.95);
  approx(fn(1, 0.4), 4.2 * 0.6 / 0.95);
  approx(fn(-1, 0), 4.2 * 0.95);
  approx(fn(0, 0), 0);
  assert.ok(fn(-1, 0) >= 0, 'discharge wear cost must never be negative');
}

const mutants = [
  {
    id: 'charge-forced-and-solar-as-equal-wear',
    before: 'Math.max(0, dE - freeSolarKwh)',
    after: 'dE',
  },
  {
    id: 'negative-discharge-wear-credit',
    before: 'BATTERY_WEAR_COST_ORE_PER_KWH * -dE * ONE_WAY_EFF',
    after: 'BATTERY_WEAR_COST_ORE_PER_KWH * dE * ONE_WAY_EFF',
  },
];

test('pure optimizer wear cost obeys independent cost invariants', () => {
  independentContract(evaluate(original));
});

for (const mutant of mutants) {
  test('mutation killed: ' + mutant.id, () => {
    assert.ok(original.includes(mutant.before), 'expected original source anchor changed');
    assert.equal(original.split(mutant.before).length, 2,
      'mutant must change exactly one expression');
    const changed = original.replace(mutant.before, mutant.after);
    assert.notEqual(changed, original);
    assert.throws(() => independentContract(evaluate(changed)),
      /independent cost evidence|discharge wear cost/);
  });
}
