/**
 * The context pack of the mock: seeds, a breadth-first neighborhood and a token budget over
 * in-memory Branch data. It follows the shape of Keeper's pack, not its exact ranking.
 */
import type { ContextPack, ContextRequest, Query, RecordRef } from '../types';
import { runQuery, runSearch, titleOf } from './engine';
import { refOf, type BranchData, type StoredRecord } from './state';

/** Characters one token stands for in the estimate; Keeper reports its own in the pack. */
const CHARS_PER_TOKEN = 4;
const DEFAULT_BUDGET_TOKENS = 4000;
const DEFAULT_DEPTH = 2;
const SEED_LIMIT = 5;

type Neighbor = ContextPack['neighbors'][number];

const tokens = (value: unknown) => Math.ceil(JSON.stringify(value).length / CHARS_PER_TOKEN);

function seedsOf(data: BranchData, request: ContextRequest): RecordRef[] {
  if (request.refs != null) return request.refs;
  if (request.search != null) {
    return runSearch(data, { q: request.search }, SEED_LIMIT).map((hit) => hit.ref);
  }
  if (request.query?.kind != null) {
    const query = { ...request.query, limit: SEED_LIMIT } as Query;
    return runQuery(data, query, SEED_LIMIT).rows.map((row) => row.ref);
  }
  return [];
}

function linksOf(
  data: BranchData,
  included: Set<RecordRef>,
  relationOk: (name: string) => boolean,
): ContextPack['links'] {
  const grouped: ContextPack['links'] = {};
  for (const link of data.links) {
    if (!relationOk(link.relation) || !included.has(link.from) || !included.has(link.to)) continue;
    (grouped[link.relation] ??= []).push({ from: link.from, to: link.to });
  }
  return grouped;
}

export function buildContext(
  domain: string,
  branch: string,
  revision: string,
  data: BranchData,
  request: ContextRequest,
): ContextPack {
  const known = new Map<RecordRef, StoredRecord>(
    data.records.map((r) => [refOf(r.kind, r.key), r]),
  );
  const seedRefs = seedsOf(data, request).filter((ref) => known.has(ref));
  const relationOk = (name: string) =>
    request.relations == null || request.relations.length === 0 || request.relations.includes(name);

  const depthOf = new Map<RecordRef, number>(seedRefs.map((ref) => [ref, 0]));
  let frontier = [...seedRefs];
  const maxDepth = request.depth ?? DEFAULT_DEPTH;
  for (let depth = 1; depth <= maxDepth; depth += 1) {
    const next: RecordRef[] = [];
    for (const current of frontier) {
      for (const link of data.links) {
        if (!relationOk(link.relation)) continue;
        const other = link.from === current ? link.to : link.to === current ? link.from : null;
        if (other === null || depthOf.has(other) || !known.has(other)) continue;
        depthOf.set(other, depth);
        next.push(other);
      }
    }
    frontier = next;
  }

  const view = (ref: RecordRef) => {
    const rec = known.get(ref);
    return {
      ref,
      title: rec === undefined ? ref : titleOf(data, rec),
      values: { ...rec?.values },
    };
  };
  const seeds = seedRefs.map(view);
  let neighbors: Neighbor[] = [...depthOf]
    .filter(([, depth]) => depth > 0)
    .map(([ref, depth]) => ({ ...view(ref), depth }));

  const limit = request.budget_tokens ?? DEFAULT_BUDGET_TOKENS;
  const trimmed: ContextPack['budget']['trimmed'] = [];
  const omitted: Record<string, number> = {};
  const measure = () => {
    const included = new Set<RecordRef>([...seedRefs, ...neighbors.map((n) => n.ref)]);
    const links = linksOf(data, included, relationOk);
    const sizes = { seeds: tokens(seeds), neighbors: tokens(neighbors), links: tokens(links) };
    return { links, sizes, used: sizes.seeds + sizes.neighbors + sizes.links };
  };

  let state = measure();
  if (state.used > limit && neighbors.length > 0) {
    // First the values of the neighbors go, then the farthest neighbors themselves.
    neighbors = neighbors.map(({ ref, title, depth }) => ({ ref, title, depth }));
    trimmed.push({ step: 'neighbor_values', removed: neighbors.length });
    state = measure();
  }
  let removed = 0;
  while (state.used > limit && neighbors.length > 0) {
    const deepest = Math.max(...neighbors.map((n) => n.depth));
    const index = neighbors.map((n) => n.depth).lastIndexOf(deepest);
    neighbors = neighbors.filter((_, i) => i !== index);
    removed += 1;
    state = measure();
  }
  if (removed > 0) {
    trimmed.push({ step: 'neighbors', removed });
    omitted['neighbors'] = removed;
  }

  return {
    domain,
    branch,
    revision,
    etag: `W/"${revision}"`,
    seeds,
    neighbors,
    links: state.links,
    budget: {
      limit_tokens: limit,
      used_tokens: state.used,
      chars_per_token: CHARS_PER_TOKEN,
      sections: state.sizes,
      trimmed,
      omitted,
      over_budget: state.used > limit,
      capped: false,
    },
  };
}
