#!/usr/bin/env node
/** A05: Read-only TypeScript reachability and internal feature-branch inventory.
 *
 * Only static source AST. No bundler execution, runtime read, environment values,
 * network, and especially no source deletion. Dynamic imports are reported as
 * potential blockers; no "unused" finding alone authorizes removal.
 */
import { readdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const PROD_FOLDERS = ['app', 'lib'];
const EXTENSIONS = ['.ts', '.tsx', '.mts', '.js', '.jsx', '/index.ts', '/index.tsx'];
const MODEL_PREFIX = /^(?:Ev|Car|Price|Energy|Dispatch|Battery|Solar|Inverter|Soc|Optimizer|Plan|Telemetry|Weather|Grid|Control)[A-Z]/;
const ENV_KEY = /^[A-Z][A-Z0-9_]{1,64}$/;

function walk(root, directory, out) {
  const folder = path.join(root, directory);
  if (!existsSync(folder)) return;
  for (const entry of readdirSync(folder, { withFileTypes: true })) {
    if (entry.isSymbolicLink()) continue;
    const relative = path.posix.join(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== '__tests__' && entry.name !== 'node_modules') walk(root, relative, out);
    } else if (entry.isFile() && /\.(?:ts|tsx|mts)$/.test(entry.name)
               && !/\.test\.(?:ts|tsx)$|\.d\.ts$/.test(entry.name)) {
      out.push(relative);
    }
  }
}

export function collectProductionFiles(root = DEFAULT_ROOT) {
  const files = [];
  for (const folder of PROD_FOLDERS) walk(root, folder, files);
  return files.sort();
}

export function isNextEntrypoint(filename) {
  return filename.startsWith('app/') &&
    /\/(?:page|layout|route|loading|error|not-found|template|default)\.(?:ts|tsx)$/.test(filename);
}

export function collectSourceFacts(text, file = 'module.ts') {
  const ast = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const imports = [];
  const dynamic = [];
  const flags = [];
  const unreachable = [];
  const models = [];

  const visit = (node) => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) &&
        node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      imports.push(node.moduleSpecifier.text);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)
               && node.moduleReference.expression && ts.isStringLiteral(node.moduleReference.expression)) {
      imports.push(node.moduleReference.expression.text);
    } else if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword ||
               ts.isIdentifier(node.expression) && (node.expression.text === 'require'))) {
      if (node.arguments[0] && ts.isStringLiteral(node.arguments[0])) imports.push(node.arguments[0].text);
      else dynamic.push(ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1);
    }
    if (ts.isPropertyAccessExpression(node) && ts.isPropertyAccessExpression(node.expression) &&
        ts.isIdentifier(node.expression.expression) && node.expression.expression.text === 'process' &&
        node.expression.name.text === 'env' && ENV_KEY.test(node.name.text)) {
      flags.push({ name: node.name.text, line: ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1 });
    }
    if (ts.isIfStatement(node) || ts.isConditionalExpression(node)) {
      const condition = node.expression ?? node.condition;
      if (condition?.kind === ts.SyntaxKind.TrueKeyword || condition?.kind === ts.SyntaxKind.FalseKeyword) {
        unreachable.push({ kind: 'constant-condition', line: ast.getLineAndCharacterOfPosition(condition.getStart(ast)).line + 1,
                           value: condition.kind === ts.SyntaxKind.TrueKeyword });
      }
    }
    if ((ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node)) &&
        node.name && MODEL_PREFIX.test(node.name.text)) {
      models.push({ name: node.name.text, line: ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1 });
    }
    ts.forEachChild(node, visit);
  };
  visit(ast);
  return { imports: [...new Set(imports)], dynamic, flags, unreachable, models };
}

export function resolveImport(filename, specifier, known) {
  if (!specifier.startsWith('.') && !specifier.startsWith('@/')) return null;
  const from = specifier.startsWith('@/')
    ? specifier.slice(2)
    : path.posix.normalize(path.posix.join(path.posix.dirname(filename), specifier));
  const candidates = [from, ...EXTENSIONS.map(ext => from + ext)];
  return candidates.find(candidate => known.has(candidate)) ?? null;
}

export function analyze(root = DEFAULT_ROOT) {
  const files = collectProductionFiles(root);
  const known = new Set(files);
  const graph = {};
  const flags = {};
  const unreachableBranches = [];
  const modelNames = new Map();
  const unresolvedLocalImports = [];
  const dynamicImports = [];
  for (const file of files) {
    const source = readFileSync(path.join(root, file), 'utf8');
    const parsed = collectSourceFacts(source, file);
    graph[file] = [];
    for (const specifier of parsed.imports) {
      const resolved = resolveImport(file, specifier, known);
      if (resolved) graph[file].push(resolved);
      else if (specifier.startsWith('@/') || specifier.startsWith('.')) {
        // Imported image, CSS, JSON and .d.ts files aren't TS module graph nodes.
        if (!/\.(?:css|scss|json|svg|png|jpg|webp|d\.ts)$/.test(specifier)) {
          unresolvedLocalImports.push({ file, specifier });
        }
      }
    }
    for (const flag of parsed.flags) (flags[flag.name] ??= []).push({ file, line: flag.line });
    for (const branch of parsed.unreachable) unreachableBranches.push({ file, ...branch });
    for (const model of parsed.models) (modelNames.get(model.name) ?? modelNames.set(model.name, []).get(model.name))
      .push({ file, line: model.line });
    for (const line of parsed.dynamic) dynamicImports.push({ file, line });
  }
  const entrypoints = files.filter(isNextEntrypoint);
  const reached = new Set();
  const todo = [...entrypoints];
  while (todo.length) {
    const item = todo.pop();
    if (reached.has(item)) continue;
    reached.add(item);
    for (const dependency of graph[item] ?? []) if (!reached.has(dependency)) todo.push(dependency);
  }

  // Track explicit references from manual CLI scripts separately. A manual-only
  // import is a retention reason, not a web-root dependency.
  const tools = [];
  walk(root, 'scripts/tools', tools);
  const manual = {};
  for (const file of tools) {
    const parsed = collectSourceFacts(readFileSync(path.join(root, file), 'utf8'), file);
    for (const specifier of parsed.imports) {
      const dependency = resolveImport(file, specifier, known);
      if (dependency) (manual[dependency] ??= []).push(file);
    }
  }
  const collisions = [...modelNames].filter(([, entries]) => entries.length > 1)
    .map(([name, entries]) => ({ name, entries })).sort((a, b) => a.name.localeCompare(b.name));
  return {
    schema: 'solinteg-ts-static-review-v1',
    source_modules: files.length,
    entrypoints,
    reachable_modules: [...reached].sort(),
    unreachable_modules: files.filter(file => !reached.has(file)).map(file => ({
      file, retained_by_manual_tools: manual[file] ?? [],
      deletion_proposed: false,
    })),
    unresolved_local_imports: unresolvedLocalImports,
    computed_dynamic_imports: dynamicImports,
    explicit_constant_conditions: unreachableBranches,
    feature_flags: flags,
    candidate_model_collisions: collisions,
    audit_limitations: [
      'Static web reachability does not prove dead execution branches',
      'Computed imports, webpack/Next virtual entrypoints and external scripts require explicit review',
      'Environment feature flags are named but their effective values are intentionally never read',
      'Model-name collisions are not proof of equivalent types',
      'No module, branch, flag or type is proposed for deletion',
    ],
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = DEFAULT_ROOT;
  const result = analyze(root);
  const index = process.argv.indexOf('--output');
  if (index >= 0) {
    if (!process.argv[index + 1]) throw new Error('--output requires a relative path');
    const target = path.resolve(process.argv[index + 1]);
    if (!target.startsWith(root + path.sep) || existsSync(target)) {
      throw new Error('output must be a new file inside the repository');
    }
    writeFileSync(target, JSON.stringify(result, null, 2) + '\n', { encoding: 'utf8', flag: 'wx', mode: 0o600 });
  } else {
    console.log(JSON.stringify({
      total: result.source_modules,
      entrypoints: result.entrypoints.length,
      reachable: result.reachable_modules.length,
      unreachable: result.unreachable_modules.length,
      computed_dynamic_imports: result.computed_dynamic_imports.length,
      constant_conditions: result.explicit_constant_conditions.length,
      feature_flags: Object.keys(result.feature_flags).length,
      model_name_collisions: result.candidate_model_collisions.length,
    }));
  }
}
