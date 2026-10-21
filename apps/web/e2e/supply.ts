/**
 * The sample Domain of examples/supply, loaded through the REST API, and the Proposals the
 * browser flows start from. The schema goes in as the document of the example with the three
 * elements that use expressions taken out (Keeper does not enable expressions yet, see
 * docs/quickstart.md); the data goes in through the built-in `import_records` and
 * `import_links` Operations, the way the `lorebase import` command does.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { parse, stringify } from 'yaml';
import type { Keeper } from './keeper';

const SAMPLE = fileURLToPath(new URL('../../../examples/supply/', import.meta.url));

type Doc = Record<string, Record<string, Record<string, unknown>>>;

/**
 * The example schema for the Domain `domain`, without its Operations, its Derivation and the
 * Attribute it computes.
 */
export function supplySchemaText(domain: string): string {
  const document = parse(readFileSync(`${SAMPLE}schema.yaml`, 'utf8')) as Doc;
  (document['domain'] as unknown as { id: string }).id = domain;
  for (const name of ['flag_supplier_risk', 'approve_contract'])
    delete document['operations']?.[name];
  delete document['derivations']?.['single_sourced'];
  const attributes = document['kinds']?.['Part']?.['attributes'] as
    Record<string, unknown> | undefined;
  if (attributes !== undefined) delete attributes['single_sourced'];
  return stringify(document, { indent: 2, lineWidth: 0 });
}

const FLAGS: Record<string, boolean> = { true: true, false: false };
const LINK_ENDS: Record<string, string> = { from_key: 'from', to_key: 'to' };

/** Rows of a CSV file as the import Operations take them: JSON booleans, empty cells left out. */
export function csvRows(file: string): Record<string, unknown>[] {
  const [header, ...lines] = readFileSync(`${SAMPLE}data/${file}.csv`, 'utf8')
    .trim()
    .split(/\r?\n/);
  const columns = (header ?? '').split(',');
  return lines.map((line) => {
    const cells = line.split(',');
    const row: Record<string, unknown> = {};
    columns.forEach((column, index) => {
      const cell = cells[index] ?? '';
      if (cell === '') return;
      row[LINK_ENDS[column] ?? column] = FLAGS[cell] ?? cell;
    });
    return row;
  });
}

const RECORDS = [
  ['Supplier', 'supplier'],
  ['Part', 'part'],
  ['Plant', 'plant'],
] as const;
const LINKS = [
  ['supplied_by', 'supplied_by'],
  ['stocked_at', 'stocked_at'],
] as const;

const base = (domain: string) => `/domains/${encodeURIComponent(domain)}`;

interface Execution {
  status: string;
  edit_count: number;
}

/** Creates the Domain `domain` with the supply schema, 90 Records and 96 Links on `main`. */
export async function loadSupply(keeper: Keeper, domain: string): Promise<void> {
  const d = base(domain);
  await keeper.post('/domains', { id: domain, display_name: 'Supply chain' });
  // `main` is protected from the start; the sample goes in directly, then it is protected again.
  await keeper.patch(`${d}/branches/main`, { protected: false });
  await keeper.post(`${d}/branches/main/schema/changes`, {
    yaml: supplySchemaText(domain),
    message: 'Apply the supply schema',
  });
  const execute = (operation: string, parameters: Record<string, unknown>) =>
    keeper.post<Execution>(`${d}/operations/${operation}/execute`, { branch: 'main', parameters });
  for (const [kind, file] of RECORDS)
    await execute('import_records', { kind, rows: csvRows(file) });
  for (const [relation, file] of LINKS)
    await execute('import_links', { relation, rows: csvRows(file) });
  await keeper.patch(`${d}/branches/main`, { protected: true });
}

export interface ConflictScenario {
  /** The Proposal that merged first (`model/risk` into `main`). */
  merged: string;
  /** The Proposal left open, with two Conflicts against the merged one. */
  open: string;
}

/**
 * Two Branches edit the same Supplier Attributes differently: `model/risk` sets the risk score
 * of S-1001 to 80 and the tier of S-1002 to 1, `source/erp` sets them to 45 and 2. The first is
 * approved and merged into `main`; the second stays open, and merging it finds two Conflicts.
 */
export async function prepareConflicts(keeper: Keeper, domain: string): Promise<ConflictScenario> {
  const d = base(domain);
  const edit = (branch: string, key: string, values: Record<string, unknown>) =>
    keeper.post(`${d}/operations/update_supplier/execute`, {
      branch,
      parameters: { key, ...values },
    });
  for (const branch of ['model/risk', 'source/erp']) {
    await keeper.post(`${d}/branches`, { name: branch, source: 'main' });
  }
  await edit('model/risk', 'S-1001', { risk_score: 80 });
  await edit('model/risk', 'S-1002', { tier: '1' });
  await edit('source/erp', 'S-1001', { risk_score: 45 });
  await edit('source/erp', 'S-1002', { tier: '2' });

  const propose = (source: string, title: string) =>
    keeper.post<{ id: string }>(`${d}/proposals`, {
      source_ref: source,
      target_ref: 'main',
      title,
    });
  const merged = await propose('model/risk', 'Risk model: score S-1001 and tier S-1002');
  await keeper.post(`${d}/proposals/${merged.id}/reviews`, { verdict: 'approve' });
  await keeper.post(`${d}/proposals/${merged.id}/merge`);
  const open = await propose('source/erp', 'ERP sync: nightly supplier run');
  return { merged: merged.id, open: open.id };
}

/** Removes the Domain of the run; the Keeper may be one somebody else started. */
export async function dropDomain(keeper: Keeper, domain: string): Promise<void> {
  await keeper.delete(`${base(domain)}?confirm=${encodeURIComponent(domain)}`);
}
