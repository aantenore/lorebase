import { parse, stringify } from 'yaml';
import type { KeeperApi } from '../client';
import { notFound, problem } from '../errors';
import type {
  InferenceEvidence,
  InferenceIssue,
  InferenceTable,
  InferRequest,
  SchemaProposalDraft,
} from '../inference-types';
import recorded from './sample/inference-supply.json';

/**
 * Mock of `POST /domains/{d}/schema:infer`. It does not read the files: it answers with the
 * recorded output of the real inference on `examples/supply/data` (the run that
 * `packages/core/tests/infer/test_supply_gold.py` checks: 6 of 6 Kinds and keys, 44 of 47
 * Attribute types, 7 of 7 Relations), narrowed to the tables the request names. The
 * evidence, confidences and issues are the engine's own.
 */

/** Secret references the mock Keeper allowlists for Postgres inference. */
export const MOCK_ALLOWED_SOURCES: readonly string[] = ['secret://env/ERP_DSN'];

type Doc = Record<string, unknown>;

interface Recorded {
  yaml: string;
  evidence: Omit<InferenceEvidence, 'element_id'>[];
  issues: InferenceIssue[];
  tables: InferenceTable[];
}

const sample = recorded as unknown as Recorded;

const isDoc = (value: unknown): value is Doc =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** The value at a dotted element path (`kinds.Supplier.attributes.tier`), or undefined. */
function at(doc: Doc, path: string): unknown {
  let node: unknown = doc;
  for (const part of path.split('.')) {
    if (!isDoc(node)) return undefined;
    node = node[part];
  }
  return node;
}

function tableNames(): string[] {
  return sample.tables.map((t) => t.table);
}

/** Table name of an uploaded file: the file name without its extension. */
function tableOfFile(name: string): string {
  return name.replace(/\.[^.]+$/, '').toLowerCase();
}

/** Keeps the Kinds and Relations whose tables were requested; dangling references degrade. */
function narrow(doc: Doc, requested: ReadonlySet<string>) {
  const keptTables = sample.tables.filter((t) => requested.has(t.table));
  const keptKinds = new Set(
    keptTables.filter((t) => t.role === 'kind').map((t) => t.element.replace('kinds.', '')),
  );
  const kinds = doc['kinds'] as Record<string, Doc>;
  const relations = doc['relations'] as Record<string, Doc>;
  for (const name of Object.keys(kinds)) if (!keptKinds.has(name)) delete kinds[name];
  const keptRelationTables = new Set(
    keptTables.filter((t) => t.role === 'relation').map((t) => t.element.replace('relations.', '')),
  );
  for (const [name, relation] of Object.entries(relations)) {
    const ends =
      keptKinds.has(String(relation['from_kind'])) && keptKinds.has(String(relation['to_kind']));
    const owned = relation['backing'] === 'foreign_key' || keptRelationTables.has(name);
    if (!ends || !owned) delete relations[name];
  }
  for (const kind of Object.values(kinds)) {
    for (const attribute of Object.values(kind['attributes'] as Record<string, Doc>)) {
      const type = attribute['type'];
      const target = typeof type === 'string' ? /^ref\((.+)\)$/.exec(type)?.[1] : undefined;
      if (target !== undefined && !keptKinds.has(target)) attribute['type'] = 'string';
    }
  }
}

function proposalFor(domain: string, requested: ReadonlySet<string>): SchemaProposalDraft {
  const doc = parse(sample.yaml) as Doc;
  doc['domain'] = { id: domain };
  const all = tableNames().every((t) => requested.has(t));
  if (!all) narrow(doc, requested);

  const issues = sample.issues.filter(
    (i) => i.element === null || at(doc, i.element) !== undefined,
  );
  const evidence = sample.evidence.flatMap((e) => {
    const element = at(doc, e.element);
    if (!isDoc(element)) return [];
    return [{ ...e, element_id: String(element['id']) }];
  });
  return {
    domain,
    status: 'proposed',
    schema: {},
    yaml: all
      ? sample.yaml.replace('id: supply', `id: ${domain}`)
      : stringify(doc, { lineWidth: 0 }),
    evidence,
    issues,
    issue_count: issues.length,
    tables: sample.tables.filter((t) => at(doc, t.element) !== undefined),
  };
}

function requestedTables(request: InferRequest): Set<string> {
  const known = new Set(tableNames());
  const invalid = (detail: string) => problem(422, 'Invalid request', detail, 'invalid_request');
  if ('files' in request) {
    if (request.files.length === 0) throw invalid('Upload at least one data file.');
    const names = request.files.map((f) => tableOfFile(f.name));
    const unknown = names.filter((n) => !known.has(n));
    if (unknown.length > 0) {
      throw invalid(
        `Mock mode knows the sample tables only (${[...known].join(', ')}); unknown: ${unknown.join(', ')}.`,
      );
    }
    return new Set(names);
  }
  if (!MOCK_ALLOWED_SOURCES.includes(request.postgres.dsn)) {
    throw invalid(
      `${request.postgres.dsn} is not in keeper.schema_inference.allowed_sources of the mock Keeper.`,
    );
  }
  const tables = request.postgres.tables ?? [];
  const unknown = tables.filter((n) => !known.has(n));
  if (unknown.length > 0) throw invalid(`Unknown tables: ${unknown.join(', ')}.`);
  return tables.length === 0 ? known : new Set(tables);
}

export interface InferenceMock {
  api: KeeperApi['inference'];
}

export function createInferenceMock(
  domainExists: (domain: string) => boolean,
  audit: (domain: string, source: 'csv' | 'postgres', tables: number, issues: number) => void,
): InferenceMock {
  return {
    api: {
      infer: (domain, request) => {
        try {
          if (!domainExists(domain)) throw notFound(`Domain ${domain}`);
          const proposal = proposalFor(domain, requestedTables(request));
          const source = 'files' in request ? 'csv' : 'postgres';
          audit(domain, source, proposal.tables.length, proposal.issue_count);
          return Promise.resolve(proposal);
        } catch (error) {
          return Promise.reject(error instanceof Error ? error : new Error(String(error)));
        }
      },
    },
  };
}
