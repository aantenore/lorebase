import type { SearchCorrection, TraceStep } from '@/api/types';

/** What the screen shows of a step's `result`; Keeper sends no values, only counts and flags. */
export interface ResultView {
  /** Counted things, in the order Keeper listed them: `items 3`, `hops 2`. */
  counts: { name: string; value: number }[];
  mode: string | null;
  corrections: SearchCorrection[];
  budget: BudgetView | null;
  truncated: boolean;
  hasMore: boolean;
}

export interface BudgetView {
  limitTokens: number;
  usedTokens: number;
  overBudget: boolean;
  capped: boolean;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const finite = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

function parseCorrections(value: unknown): SearchCorrection[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry: unknown) => {
    if (!isRecord(entry) || typeof entry['word'] !== 'string') return [];
    const replacements = Array.isArray(entry['replacements'])
      ? entry['replacements'].filter((r): r is string => typeof r === 'string')
      : [];
    return [{ word: entry['word'], replacements }];
  });
}

function parseBudget(value: unknown): BudgetView | null {
  if (!isRecord(value) || !finite(value['limit_tokens']) || !finite(value['used_tokens'])) {
    return null;
  }
  return {
    limitTokens: value['limit_tokens'],
    usedTokens: value['used_tokens'],
    overBudget: value['over_budget'] === true,
    capped: value['capped'] === true,
  };
}

/** Reads a step's `result` defensively: unknown shapes show as less, never as an error. */
export function parseResult(result: TraceStep['result']): ResultView {
  const raw = isRecord(result) ? result : {};
  const counts = isRecord(raw['counts'])
    ? Object.entries(raw['counts']).flatMap(([name, value]) =>
        finite(value) ? [{ name, value }] : [],
      )
    : [];
  return {
    counts,
    mode: typeof raw['mode'] === 'string' ? raw['mode'] : null,
    corrections: parseCorrections(raw['corrections']),
    budget: parseBudget(raw['budget']),
    truncated: raw['truncated'] === true,
    hasMore: raw['has_more'] === true,
  };
}

/** `items 3, hops 2`; empty text when nothing was counted. */
export function countsSummary(counts: ResultView['counts']): string {
  return counts.map(({ name, value }) => `${name} ${value.toString()}`).join(', ');
}

/** `beerings -> bearings, bearing`. */
export function correctionText(correction: SearchCorrection): string {
  return `${correction.word} -> ${correction.replacements.join(', ')}`;
}

/** Share of the budget used, 0 to 1 (capped at 1 for the bar). */
export function budgetShare(budget: BudgetView): number {
  return budget.limitTokens <= 0 ? 1 : Math.min(1, budget.usedTokens / budget.limitTokens);
}

/** The query as indented JSON text, or null when the step carried none. */
export function queryText(query: TraceStep['query']): string | null {
  if (query === null || query === undefined || Object.keys(query).length === 0) return null;
  return JSON.stringify(query, null, 2);
}

/** `2026-10-20T09:14:02Z` as `09:14:02`; anything else is returned as it is. */
export function clockTime(iso: string): string {
  return /T(\d{2}:\d{2}:\d{2})/.exec(iso)?.[1] ?? iso;
}
