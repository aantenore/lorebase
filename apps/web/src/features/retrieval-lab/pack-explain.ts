import type { ContextPack } from '@/api/types';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';

const P = retrievalLabMessages.pack;

/** A Record of a context pack with the reason it is there, worked out from the pack itself. */
export interface PackEntry {
  ref: string;
  title: string;
  role: 'seed' | 'neighbor';
  /** 0 for a seed. */
  depth: number;
  why: string;
  /** The neighbor's values were left out to fit the budget. */
  valuesTrimmed: boolean;
}

interface Hop {
  relation: string;
  from: string;
  inverse: boolean;
}

/** The first Link of the pack that joins `ref` to a Record one step closer to the seeds. */
function hopTo(
  pack: ContextPack,
  ref: string,
  depth: number,
  depths: Map<string, number>,
): Hop | null {
  for (const [relation, links] of Object.entries(pack.links)) {
    for (const link of links) {
      if (link.to === ref && depths.get(link.from) === depth - 1) {
        return { relation, from: link.from, inverse: false };
      }
      if (link.from === ref && depths.get(link.to) === depth - 1) {
        return { relation, from: link.to, inverse: true };
      }
    }
  }
  return null;
}

/**
 * Why each Record is in the pack: a seed matched the search; a neighbor of depth N was reached
 * through a Link of the pack from a Record of depth N-1.
 */
export function explainPack(pack: ContextPack, question: string): PackEntry[] {
  const depths = new Map<string, number>();
  for (const seed of pack.seeds) depths.set(seed.ref, 0);
  for (const neighbor of pack.neighbors) depths.set(neighbor.ref, neighbor.depth);

  const seeds = pack.seeds.map<PackEntry>((seed) => ({
    ref: seed.ref,
    title: seed.title ?? seed.ref,
    role: 'seed',
    depth: 0,
    why: P.matched(question),
    valuesTrimmed: false,
  }));
  const neighbors = pack.neighbors.map<PackEntry>((neighbor) => {
    const hop = hopTo(pack, neighbor.ref, neighbor.depth, depths);
    const reached = hop === null ? P.notInPack : P.reachedVia(hop.relation, hop.from, hop.inverse);
    return {
      ref: neighbor.ref,
      title: neighbor.title ?? neighbor.ref,
      role: 'neighbor',
      depth: neighbor.depth,
      why: P.depth(neighbor.depth, reached),
      valuesTrimmed: neighbor.values == null,
    };
  });
  return [...seeds, ...neighbors];
}

export interface BudgetSection {
  name: string;
  tokens: number;
  /** Share of the tokens used, 0 to 1. */
  share: number;
}

export interface BudgetReport {
  used: number;
  limit: number;
  /** Share of the limit used, 0 to 1 and over. */
  usage: number;
  sections: BudgetSection[];
  /** What was cut to fit: the trimming steps in order and the entries dropped per section. */
  trimmed: string[];
  omitted: string[];
  overBudget: boolean;
  capped: boolean;
}

/** Token usage against the budget, per section, with what the budget cost. */
export function budgetReport(budget: ContextPack['budget']): BudgetReport {
  const used = budget.used_tokens;
  return {
    used,
    limit: budget.limit_tokens,
    usage: budget.limit_tokens <= 0 ? 1 : used / budget.limit_tokens,
    sections: Object.entries(budget.sections).map(([name, tokens]) => ({
      name,
      tokens,
      share: used <= 0 ? 0 : tokens / used,
    })),
    trimmed: budget.trimmed.map((t) => P.removed(t.step, t.removed)),
    omitted: Object.entries(budget.omitted)
      .filter(([, count]) => count > 0)
      .map(([section, count]) => P.leftOut(section, count)),
    overBudget: budget.over_budget,
    capped: budget.capped,
  };
}
