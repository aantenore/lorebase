import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

/**
 * The app is domain-agnostic: screens, routes and shared code never name the Kinds, Records or
 * Domains of a dataset, and never put a Domain's name into generic copy. Sample data lives in
 * the mock fixtures (src/api/mock, src/api/live/fixtures) and in tests, which this check skips.
 */

const ROOT = join(import.meta.dirname, '..');

const SKIPPED: readonly RegExp[] = [
  /^api\/mock\//,
  /^api\/live\/fixtures\//,
  /^api\/generated\//,
  /^test\//,
  /\.test\.tsx?$/,
  /^routeTree\.gen\.ts$/,
];

/** Names from the example datasets (examples/supply and the mock Domains). */
const EXAMPLE_IDENTIFIERS: readonly [string, RegExp][] = [
  ['the supply Domain', /\bsuppl(y|ies|ier|iers|ied)\b/i],
  ['an example Kind', /\b(Part|Plant|PurchaseOrder|RiskFlag|Contract)\b/],
  ['an example Record key', /\b(S-1\d{3}|P-\d{5}|C-3\d{3}|PO-\d{4,})\b/],
  ['an example attribute', /\b(supplier_code|part_number|risk_score|legal_name|single_sourced)\b/],
  [
    'an example relation',
    /\b(supplied_by|stocked_at|issued_to|for_part|delivered_to|with_supplier)\b/,
  ],
  [
    'example data',
    /\b(bearings?|bering|beerings|Torino|Corvane|Brevik|Kestrel|Orlova|Wexrix|Kavvik|Tamura)\b/i,
  ],
  ['a condition on a Domain id', /\bdomain(Id)?\s*[!=]==?\s*['"`]/],
];

/** A Domain's display name interpolated into a string: copy must stay generic. */
const DOMAIN_NAME_IN_COPY = /\$\{[^}]*\b(domainInfo|domain|domainData)\??\.display_name/;

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sources(path);
    return /\.(ts|tsx)$/.test(entry.name) ? [path] : [];
  });
}

const checked = sources(ROOT)
  .map((path) => ({ path, name: relative(ROOT, path).split(sep).join('/') }))
  .filter(({ name }) => !SKIPPED.some((pattern) => pattern.test(name)));

function findings(pattern: RegExp): string[] {
  return checked.flatMap(({ path, name }) =>
    readFileSync(path, 'utf8')
      .split('\n')
      .flatMap((line, index) =>
        pattern.test(line) ? [`${name}:${(index + 1).toString()}: ${line.trim()}`] : [],
      ),
  );
}

describe('domain-agnostic app code', () => {
  it('checks the screens, routes and messages', () => {
    const names = checked.map((c) => c.name);
    expect(names).toContain('messages/index.ts');
    expect(names.some((n) => n.startsWith('features/'))).toBe(true);
    expect(names.some((n) => n.startsWith('routes/'))).toBe(true);
  });

  it.each(EXAMPLE_IDENTIFIERS)('never names %s', (_what, pattern) => {
    expect(findings(pattern)).toEqual([]);
  });

  it('never puts a Domain name into copy', () => {
    expect(findings(DOMAIN_NAME_IN_COPY)).toEqual([]);
  });
});
