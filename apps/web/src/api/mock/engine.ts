/**
 * Pure read-path logic for the mock adapter: a small interpreter for the Query DSL
 * (design 8.2), lexical search, neighborhoods and diffs over in-memory Branch data.
 */
import { notSupported, problem } from '../errors';
import type {
  AttributeChange,
  ChangeType,
  DiffChange,
  DiffResponse,
  GraphEdge,
  GraphNode,
  Kind,
  NeighborhoodRequest,
  OrderBy,
  Query,
  RecordRow,
  SearchRequest,
  SearchResult,
  Where,
} from '../types';
import { refOf, splitRef, type BranchData, type StoredLink, type StoredRecord } from './state';

// ------------------------------------------------------------ record views

export function titleOf(data: BranchData, rec: StoredRecord): string {
  const kind = data.schema.kinds.find((k) => k.api_name === rec.kind);
  const titleAttr = kind === undefined ? undefined : kind.attributes[kind.title_attribute];
  const value = titleAttr === undefined ? undefined : rec.values[titleAttr.api_name];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : rec.key;
}

export function toRow(data: BranchData, rec: StoredRecord, select?: string[]): RecordRow {
  const pick = (source: Record<string, unknown>) =>
    select === undefined
      ? { ...source }
      : Object.fromEntries(Object.entries(source).filter(([name]) => select.includes(name)));
  return {
    ref: refOf(rec.kind, rec.key),
    kind: rec.kind,
    key: rec.key,
    title: titleOf(data, rec),
    attributes: pick(rec.values),
    derived: pick(rec.derived),
  };
}

export function findKind(data: BranchData, name: string): Kind {
  const kind = data.schema.kinds.find((k) => k.api_name === name);
  if (kind === undefined)
    throw problem(404, 'Not found', `Kind ${name} does not exist.`, 'unknown_kind');
  return kind;
}

export function findRecord(data: BranchData, kind: string, key: string): StoredRecord {
  const found = data.records.find((r) => r.kind === kind && r.key === key);
  if (found === undefined) {
    throw problem(404, 'Not found', `Record ${refOf(kind, key)} does not exist.`, 'unknown_record');
  }
  return found;
}

// ------------------------------------------------------------------- where

/** Records reachable from `rec` through `name` (a forward or inverse Relation name). */
function neighborsVia(data: BranchData, rec: StoredRecord, name: string): StoredRecord[] {
  const self = refOf(rec.kind, rec.key);
  const targets: string[] = [];
  for (const l of data.links) {
    const relation = data.schema.relations.find((r) => r.api_name === l.relation);
    if (relation === undefined) continue;
    if (relation.api_name === name && l.from === self) targets.push(l.to);
    if (relation.inverse_api_name === name && l.to === self) targets.push(l.from);
  }
  return targets.flatMap((ref) => {
    const { kind, key } = splitRef(ref);
    const found = data.records.find((r) => r.kind === kind && r.key === key);
    return found === undefined ? [] : [found];
  });
}

/** Plain-text form of an Attribute value, for comparison and matching. */
function text(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return JSON.stringify(value);
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return text(a).localeCompare(text(b));
}

function applyOperator(op: string, actual: unknown, expected: unknown): boolean {
  switch (op) {
    case 'eq':
      return actual === expected;
    case 'ne':
      return actual !== expected;
    case 'lt':
      return actual != null && compare(actual, expected) < 0;
    case 'lte':
      return actual != null && compare(actual, expected) <= 0;
    case 'gt':
      return actual != null && compare(actual, expected) > 0;
    case 'gte':
      return actual != null && compare(actual, expected) >= 0;
    case 'in':
      return Array.isArray(expected) && expected.includes(actual);
    case 'between':
      return (
        Array.isArray(expected) &&
        actual != null &&
        compare(actual, expected[0]) >= 0 &&
        compare(actual, expected[1]) <= 0
      );
    case 'contains':
      return Array.isArray(actual) && actual.includes(expected);
    case 'prefix':
      return (
        typeof actual === 'string' && typeof expected === 'string' && actual.startsWith(expected)
      );
    case 'exists':
      return (actual != null) === (expected !== false);
    case 'match':
      return (
        typeof expected === 'string' && text(actual).toLowerCase().includes(expected.toLowerCase())
      );
    default:
      throw notSupported(`The "${op}" operator`);
  }
}

export function matchesWhere(
  data: BranchData,
  rec: StoredRecord,
  where: Where | undefined,
): boolean {
  if (where === undefined) return true;
  if ('and' in where) return where.and.every((w) => matchesWhere(data, rec, w));
  if ('or' in where) return where.or.some((w) => matchesWhere(data, rec, w));
  if ('not' in where) return !matchesWhere(data, rec, where.not);
  if ('via' in where) {
    return neighborsVia(data, rec, where.via).some((n) => matchesWhere(data, n, where.where));
  }
  if ('derived' in where) return applyOperator(where.op, rec.derived[where.derived], where.value);
  return applyOperator(where.op, rec.values[where.attr], where.value);
}

// ------------------------------------------------------------------- query

export function encodeCursor(offset: number): string {
  return btoa(String(offset));
}

export function decodeCursor(cursor: string | null | undefined): number {
  if (cursor === null || cursor === undefined || cursor === '') return 0;
  const offset = Number(atob(cursor));
  if (!Number.isInteger(offset) || offset < 0)
    throw problem(400, 'Bad request', 'Invalid cursor.', 'bad_cursor');
  return offset;
}

function sortRecords(rows: StoredRecord[], orderBy: OrderBy[] | undefined): StoredRecord[] {
  const sorted = [...rows];
  sorted.sort((a, b) => {
    for (const { attr, dir } of orderBy ?? []) {
      const delta = compare(a.values[attr] ?? a.derived[attr], b.values[attr] ?? b.derived[attr]);
      if (delta !== 0) return dir === 'desc' ? -delta : delta;
    }
    return a.key.localeCompare(b.key);
  });
  return sorted;
}

export function runQuery(
  data: BranchData,
  query: Query,
  fallbackLimit: number,
): { rows: RecordRow[]; next_cursor: string | null } {
  if (query.kind === undefined) throw notSupported('Trait queries');
  findKind(data, query.kind);
  const matching = data.records.filter(
    (r) => r.kind === query.kind && matchesWhere(data, r, query.where),
  );
  const ordered = sortRecords(matching, query.order_by);
  const offset = decodeCursor(query.cursor);
  const limit = query.limit ?? fallbackLimit;
  const page = ordered.slice(offset, offset + limit);
  const next = offset + limit < ordered.length ? encodeCursor(offset + limit) : null;
  return { rows: page.map((r) => toRow(data, r, query.select)), next_cursor: next };
}

// ------------------------------------------------------------------ search

function snippetFor(rec: StoredRecord, terms: string[]): string {
  for (const [name, value] of Object.entries(rec.values)) {
    const shown = text(value);
    if (terms.some((t) => shown.toLowerCase().includes(t))) return `${name}: ${shown}`;
  }
  return '';
}

/** Edit distance, capped: enough to tell a typo from another word. */
function editDistance(a: string, b: string): number {
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    const row = [i];
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      row.push(
        Math.min((previous[j] ?? 0) + 1, (row[j - 1] ?? 0) + 1, (previous[j - 1] ?? 0) + cost),
      );
    }
    previous = row;
  }
  return previous[b.length] ?? 0;
}

/** Words of the Records, for typo corrections. */
function vocabulary(data: BranchData): Set<string> {
  const words = new Set<string>();
  for (const rec of data.records) {
    const parts = [titleOf(data, rec), ...Object.values(rec.values).map((v) => text(v))];
    for (const part of parts) {
      for (const word of part.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
        if (word.length > 3) words.add(word);
      }
    }
  }
  return words;
}

/** A pseudo similarity in [0, 1) that depends on the vector and the Record, for the mock. */
function similarity(vector: number[], ref: string): number {
  let hash = vector.reduce((sum, v, i) => sum + v * (i + 1), 0);
  for (let i = 0; i < ref.length; i += 1) hash = (hash * 31 + ref.charCodeAt(i)) % 9973;
  return Math.abs(hash % 1000) / 1000;
}

export interface MockSearch {
  results: SearchResult[];
  mode: string;
  corrections: { word: string; replacements: string[] }[];
}

/**
 * Lexical search with typo corrections; a query vector makes semantic and hybrid modes add a
 * stand-in similarity. `tuning` is honored loosely: the weights scale the two scores and
 * `candidates` caps the hits.
 */
export function runSearchFull(
  data: BranchData,
  request: SearchRequest,
  fallbackLimit: number,
): MockSearch {
  const vector = request.query_vector;
  const asked = request.mode ?? (vector === undefined ? 'lexical' : 'hybrid');
  const mode = vector === undefined ? 'lexical' : asked;
  const tuning = request.tuning ?? {};
  const lexicalWeight = tuning.lexical_weight ?? 1;
  const semanticWeight = tuning.semantic_weight ?? 1;

  const asTyped = request.q.toLowerCase().split(/\s+/).filter(Boolean);
  const known = vocabulary(data);
  const corrections: MockSearch['corrections'] = [];
  const terms = asTyped.map((term) => {
    if (term.length <= 3 || known.has(term) || [...known].some((w) => w.includes(term))) {
      return term;
    }
    const close = [...known].filter((w) => editDistance(w, term) <= 2).slice(0, 2);
    if (close.length === 0) return term;
    corrections.push({ word: term, replacements: close });
    return close[0] ?? term;
  });
  if (terms.length === 0) return { results: [], mode, corrections };

  const results: SearchResult[] = [];
  for (const rec of data.records) {
    if (
      request.kinds !== undefined &&
      request.kinds.length > 0 &&
      !request.kinds.includes(rec.kind)
    )
      continue;
    if (!matchesWhere(data, rec, request.where)) continue;
    const title = titleOf(data, rec).toLowerCase();
    const body = Object.values(rec.values).map((v) => text(v).toLowerCase());
    let lexical = 0;
    for (const term of terms) {
      if (title.includes(term)) lexical += 3;
      else if (body.some((b) => b.includes(term))) lexical += 1;
    }
    const ref = refOf(rec.kind, rec.key);
    const semantic = vector === undefined || mode === 'lexical' ? 0 : similarity(vector, ref);
    const score =
      mode === 'semantic'
        ? semantic * semanticWeight
        : lexical * lexicalWeight + semantic * semanticWeight;
    if (mode === 'semantic' ? semantic < 0.5 : lexical === 0) continue;
    results.push({
      ref,
      kind: rec.kind,
      title: titleOf(data, rec),
      snippet: snippetFor(rec, terms),
      score: Math.round(score * 1000) / 1000,
    });
  }
  results.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title));
  const cap = Math.min(request.limit ?? fallbackLimit, tuning.candidates ?? Infinity);
  return { results: results.slice(0, cap), mode, corrections };
}

export function runSearch(
  data: BranchData,
  request: SearchRequest,
  fallbackLimit: number,
): SearchResult[] {
  return runSearchFull(data, request, fallbackLimit).results;
}

// ------------------------------------------------------------ neighborhood

export function neighborhood(
  data: BranchData,
  request: NeighborhoodRequest,
  fallbackMaxNodes: number,
): { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean } {
  const maxNodes = request.max_nodes ?? fallbackMaxNodes;
  const kindOk = (kind: string) => request.kinds === undefined || request.kinds.includes(kind);
  const relationOk = (name: string) =>
    request.relations === undefined || request.relations.includes(name);
  const known = new Map(data.records.map((r) => [refOf(r.kind, r.key), r]));

  const included = new Set<string>();
  let truncated = false;
  let frontier: string[] = [];
  for (const seed of request.seeds) {
    if (known.has(seed) && !included.has(seed)) {
      if (included.size >= maxNodes) truncated = true;
      else {
        included.add(seed);
        frontier.push(seed);
      }
    }
  }

  for (let hop = 0; hop < request.depth && frontier.length > 0; hop += 1) {
    const next: string[] = [];
    for (const current of frontier) {
      for (const l of data.links) {
        if (!relationOk(l.relation)) continue;
        const other = l.from === current ? l.to : l.to === current ? l.from : null;
        if (other === null || included.has(other)) continue;
        const target = known.get(other);
        if (target === undefined || !kindOk(target.kind)) continue;
        if (included.size >= maxNodes) {
          truncated = true;
          continue;
        }
        included.add(other);
        next.push(other);
      }
    }
    frontier = next;
  }

  const nodes: GraphNode[] = [...included].flatMap((ref) => {
    const rec = known.get(ref);
    return rec === undefined ? [] : [{ ref, kind: rec.kind, title: titleOf(data, rec) }];
  });
  const edges: GraphEdge[] = data.links
    .filter((l) => included.has(l.from) && included.has(l.to) && relationOk(l.relation))
    .map((l) => ({ from: l.from, to: l.to, relation: l.relation }));
  return { nodes, edges, truncated };
}

// -------------------------------------------------------------------- diff

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

function attributeChanges(
  before: Record<string, unknown>,
  after: Record<string, unknown>,
): AttributeChange[] {
  const names = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...names]
    .filter((name) => !same(before[name], after[name]))
    .map((attribute) => ({
      attribute,
      before: before[attribute] ?? null,
      after: after[attribute] ?? null,
    }));
}

function linkKey(l: StoredLink): string {
  return `${l.relation}:${l.from}>${l.to}`;
}

function diffSchemaElements(
  label: string,
  from: { api_name: string }[],
  to: { api_name: string }[],
): DiffChange[] {
  const before = new Map(from.map((e) => [e.api_name, e]));
  const after = new Map(to.map((e) => [e.api_name, e]));
  const changes: DiffChange[] = [];
  for (const [name, element] of after) {
    const old = before.get(name);
    if (old === undefined)
      changes.push({ element: 'schema', change: 'added', kind: label, ref: name });
    else if (!same(old, element))
      changes.push({ element: 'schema', change: 'modified', kind: label, ref: name });
  }
  for (const name of before.keys()) {
    if (!after.has(name))
      changes.push({ element: 'schema', change: 'removed', kind: label, ref: name });
  }
  return changes;
}

/** Diff of two Branch heads, schema and data, filtered to `kinds` when given. */
export function diffBranches(
  from: BranchData,
  to: BranchData,
  labels: { from: string; to: string },
  kinds: string[] | undefined,
): DiffResponse {
  const changes: DiffChange[] = [
    ...diffSchemaElements('Kind', from.schema.kinds, to.schema.kinds),
    ...diffSchemaElements('Relation', from.schema.relations, to.schema.relations),
    ...diffSchemaElements('Operation', from.schema.operations, to.schema.operations),
  ];

  const before = new Map(from.records.map((r) => [refOf(r.kind, r.key), r]));
  const after = new Map(to.records.map((r) => [refOf(r.kind, r.key), r]));
  for (const [ref, rec] of after) {
    const old = before.get(ref);
    if (old === undefined)
      changes.push({ element: 'record', change: 'added', kind: rec.kind, ref });
    else {
      const attributes = attributeChanges(old.values, rec.values);
      if (attributes.length > 0)
        changes.push({ element: 'record', change: 'modified', kind: rec.kind, ref, attributes });
    }
  }
  for (const [ref, rec] of before) {
    if (!after.has(ref))
      changes.push({ element: 'record', change: 'removed', kind: rec.kind, ref });
  }

  const linksBefore = new Map(from.links.map((l) => [linkKey(l), l]));
  const linksAfter = new Map(to.links.map((l) => [linkKey(l), l]));
  for (const [key, l] of linksAfter) {
    if (!linksBefore.has(key))
      changes.push({ element: 'link', change: 'added', kind: l.relation, ref: key });
  }
  for (const [key, l] of linksBefore) {
    if (!linksAfter.has(key))
      changes.push({ element: 'link', change: 'removed', kind: l.relation, ref: key });
  }

  const visible =
    kinds === undefined || kinds.length === 0
      ? changes
      : changes.filter((c) => kinds.includes(c.kind));
  const summary: Record<string, Record<ChangeType, number>> = {};
  for (const c of visible) {
    const bucket = (summary[c.kind] ??= { added: 0, modified: 0, removed: 0 });
    bucket[c.change] += 1;
  }
  return { from: labels.from, to: labels.to, summary, changes: visible, next_cursor: null };
}
