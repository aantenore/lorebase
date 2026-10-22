/**
 * Records, Links, search, traversal and diffs: Keeper responses as the view models of the
 * screens. Keeper names a Record by `kind` and `key` and serves values by Attribute api name;
 * the screens use the external reference `Kind/key`.
 */
import type { components } from '../generated/keeper';
import { refOf } from '../refs';
import type {
  AttributeChange,
  DiffChange,
  DiffResponse,
  GraphEdge,
  GraphNode,
  LinkedRecord,
  LinksResponse,
  NeighborhoodResponse,
  QueryResponse,
  RecordPage,
  RecordResponse,
  RecordRow,
  SearchResponse,
  TraverseResponse,
} from '../types';

type S = components['schemas'];

export function mapRecord(out: S['RecordOut']): RecordRow {
  return {
    ref: refOf(out.kind, out.key),
    kind: out.kind,
    key: out.key,
    title: out.title ?? out.key,
    attributes: out.values,
    // Keeper serves derived values inline with the stored ones; the screens fall back from
    // `attributes` to `derived`, so nothing needs to be split out here.
    derived: {},
  };
}

export function mapRecordPage(out: S['RecordPage']): RecordPage {
  return {
    revision: out.revision,
    rows: out.items.map(mapRecord),
    next_cursor: out.next_cursor ?? null,
  };
}

export function mapQuery(out: S['QueryResponse']): QueryResponse {
  return {
    revision: out.revision,
    rows: out.items.map(mapRecord),
    next_cursor: out.next_cursor ?? null,
  };
}

export function mapRecordResponse(out: S['RecordResponse']): RecordResponse {
  return { revision: out.revision, record: mapRecord(out.record) };
}

/** Links of one Record along `relation`; the far end is `target`, or the other key of the Link. */
export function mapLinks(out: S['LinkPage'], relation: string): LinksResponse {
  const links: LinkedRecord[] = out.items.map((item) => {
    const far = item.direction === 'forward' ? item.to_key : item.from_key;
    return {
      ref: item.target ? refOf(item.target.kind, item.target.key) : refOf(relation, far),
      title: item.target?.title ?? far,
      attributes: item.values,
    };
  });
  return {
    revision: out.revision,
    relation,
    direction: out.items[0]?.direction ?? 'forward',
    links,
    next_cursor: out.next_cursor ?? null,
  };
}

/** Keeper marks the matched words in the snippet with `<b>`; the screens show plain text. */
const stripMarks = (snippet: string) => snippet.replace(/<\/?b>/g, '');

export function mapSearch(out: S['SearchResponse']): SearchResponse {
  return {
    revision: out.revision,
    results: out.items.map((hit) => ({
      ref: refOf(hit.kind, hit.key),
      kind: hit.kind,
      title: hit.title ?? hit.key,
      snippet: stripMarks(hit.snippet ?? ''),
      score: hit.score,
    })),
    next_cursor: null,
  };
}

const nodeOf = (out: { kind: string; key: string; title?: string | null }): GraphNode => ({
  ref: refOf(out.kind, out.key),
  kind: out.kind,
  title: out.title ?? out.key,
});

export function mapNeighborhood(out: S['NeighborhoodResponse']): NeighborhoodResponse {
  const edges: GraphEdge[] = out.edges.map((edge) => ({
    from: refOf(edge.from.kind, edge.from.key),
    to: refOf(edge.to.kind, edge.to.key),
    relation: edge.relation,
  }));
  return {
    revision: out.revision,
    nodes: out.nodes.map(nodeOf),
    edges,
    truncated: out.truncated ?? false,
  };
}

export function mapTraverse(out: S['TraverseResponse']): TraverseResponse {
  const seen = new Set<string>();
  const records: RecordRow[] = [];
  for (const hop of out.hops) {
    for (const record of hop.records) {
      const row = mapRecord(record);
      if (seen.has(row.ref)) continue;
      seen.add(row.ref);
      records.push(row);
    }
  }
  return { revision: out.revision, records };
}

// --------------------------------------------------------------------- diff

const ELEMENT = { schema: 'schema', kind: 'record', relation: 'link' } as const;

function mapChange(change: S['ChangeOut']): DiffChange {
  const element = ELEMENT[change.element];
  let kind: string;
  let ref: string;
  if (element === 'record') {
    kind = change.kind ?? '';
    ref = refOf(kind, change.key ?? '');
  } else if (element === 'link') {
    kind = change.relation ?? '';
    ref = `${change.from_key ?? ''} -> ${change.to_key ?? ''}`;
  } else {
    kind = change.element_type ?? 'schema';
    ref = change.api_name ?? change.key ?? '';
  }
  const attributes: AttributeChange[] | undefined = change.changes?.map((c) => ({
    attribute: c.name,
    before: c.old ?? null,
    after: c.new ?? null,
  }));
  return {
    element,
    change: change.change,
    kind,
    ref,
    ...(attributes === undefined ? {} : { attributes }),
  };
}

export function mapDiff(out: S['ChangesPage']): DiffResponse {
  const summary: DiffResponse['summary'] = {};
  for (const part of out.summary) {
    summary[part.name] = {
      added: part.added ?? 0,
      modified: part.modified ?? 0,
      removed: part.removed ?? 0,
    };
  }
  return {
    from: out.from_revision ?? '',
    to: out.to_revision,
    summary,
    changes: out.items.map(mapChange),
    next_cursor: out.next_cursor ?? null,
  };
}
