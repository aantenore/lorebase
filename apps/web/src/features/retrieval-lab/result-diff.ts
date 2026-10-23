import type { RecordRef, SearchResult } from '@/api/types';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';

const R = retrievalLabMessages.results;

/** Where a Record ranks in each of the two result lists (1 is the best); null when absent. */
export interface RankRow {
  ref: RecordRef;
  rankA: number | null;
  rankB: number | null;
  /** Places gained in B over A (positive: B ranks it higher); null unless in both. */
  moved: number | null;
}

export interface Comparison {
  rows: Map<RecordRef, RankRow>;
  /** Present in both lists. */
  shared: number;
  onlyA: number;
  onlyB: number;
  /** Shared Records that sit at a different rank in B. */
  reordered: number;
}

const ranks = (results: SearchResult[]) => new Map(results.map((r, index) => [r.ref, index + 1]));

/** Overlap and rank changes between the results of two parameter sets. */
export function compareResults(a: SearchResult[], b: SearchResult[]): Comparison {
  const inA = ranks(a);
  const inB = ranks(b);
  const rows = new Map<RecordRef, RankRow>();
  for (const ref of new Set([...inA.keys(), ...inB.keys()])) {
    const rankA = inA.get(ref) ?? null;
    const rankB = inB.get(ref) ?? null;
    rows.set(ref, {
      ref,
      rankA,
      rankB,
      moved: rankA !== null && rankB !== null ? rankA - rankB : null,
    });
  }
  const all = [...rows.values()];
  return {
    rows,
    shared: all.filter((r) => r.rankA !== null && r.rankB !== null).length,
    onlyA: all.filter((r) => r.rankB === null).length,
    onlyB: all.filter((r) => r.rankA === null).length,
    reordered: all.filter((r) => r.moved !== null && r.moved !== 0).length,
  };
}

/** `only in A`, `same rank`, `up 2 in B`, `down 1 in B`: how a Record fares in the other set. */
export function describeRow(row: RankRow, side: 'A' | 'B'): string {
  const other = side === 'A' ? 'B' : 'A';
  const there = side === 'A' ? row.rankB : row.rankA;
  if (there === null) return R.onlyIn(side);
  if (row.moved === null || row.moved === 0) return R.sameRank(other);
  // `moved` is A's rank minus B's: positive means B ranks the Record higher.
  const gain = side === 'B' ? row.moved : -row.moved;
  return gain > 0 ? R.higher(gain, other) : R.lower(-gain, other);
}
