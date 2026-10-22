import { splitRecordRef } from '@/api/refs';
import type { Kind, RecordRef } from '@/api/types';
import { colorOf, type GraphData } from './graph-model';

/** A Link between two Records, by `Kind/key` reference. */
export interface RefLink {
  relation: string;
  from: RecordRef;
  to: RecordRef;
}

/** The id of the edge a Link becomes; the same Link always gets the same id. */
export function edgeId(link: RefLink): string {
  return `${link.relation}|${link.from}|${link.to}`;
}

/**
 * A graph of the given Records and Links. A Link whose end is not in `records` brings that end
 * along, so no edge hangs loose. Nodes keep the order they were first named in.
 */
export function refGraph(records: Iterable<RecordRef>, links: RefLink[], kinds: Kind[]): GraphData {
  const nodes = new Set<RecordRef>(records);
  for (const link of links) {
    nodes.add(link.from);
    nodes.add(link.to);
  }
  const edges = new Map<string, RefLink>(links.map((link) => [edgeId(link), link]));
  return {
    nodes: [...nodes].map((ref) => {
      const { kind } = splitRecordRef(ref);
      return { id: ref, label: ref, group: kind, color: colorOf(kinds, kind) };
    }),
    edges: [...edges].map(([id, link]) => ({
      id,
      source: link.from,
      target: link.to,
      label: link.relation,
    })),
  };
}
