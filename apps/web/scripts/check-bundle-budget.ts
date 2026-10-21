/**
 * Fails when a production build exceeds `bundle-budget.json`.
 *
 * Run with `pnpm bundle:check [dist-folder]` after `vite build` (`pnpm build` does both).
 * Node 24 runs this file directly.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { gzipSync } from 'node:zlib';
import { budgetViolations, initialScripts, parseBudget, type ChunkSize } from './bundle-budget.ts';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, process.argv[2] ?? 'dist');

function measure(servedPath: string): ChunkSize {
  const content = readFileSync(resolve(dist, servedPath.replace(/^\//, '')));
  return { file: servedPath, bytes: content.length, gzipBytes: gzipSync(content).length };
}

const budget = parseBudget(
  JSON.parse(readFileSync(resolve(root, 'bundle-budget.json'), 'utf8')) as unknown,
);
const scripts = initialScripts(readFileSync(resolve(dist, 'index.html'), 'utf8'));
const main = measure(scripts.entry);
const initial = [main, ...scripts.preloads.map(measure)];

const kb = (bytes: number) => (bytes / 1000).toFixed(2);
const total = initial.reduce((sum, chunk) => sum + chunk.bytes, 0);
const totalGzip = initial.reduce((sum, chunk) => sum + chunk.gzipBytes, 0);
console.log(
  `main chunk ${main.file}: ${kb(main.bytes)} kB (${kb(main.gzipBytes)} kB gzip); ` +
    `first load ${initial.length} files, ${kb(total)} kB (${kb(totalGzip)} kB gzip).`,
);

const violations = budgetViolations(budget, main, initial);
if (violations.length > 0) {
  for (const violation of violations) console.error(violation);
  console.error('Shrink the bundle, or raise the limit in bundle-budget.json on purpose.');
  process.exit(1);
}
