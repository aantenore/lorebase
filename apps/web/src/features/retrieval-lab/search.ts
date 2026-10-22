import { readText } from '@/app/scope';

/** Search params of the Retrieval lab: the question asked and the pack budget in tokens. */
export interface LabSearch {
  q?: string | undefined;
  budget?: number | undefined;
}

export function validateLabSearch(raw: Record<string, unknown>): LabSearch {
  const budget = Number(readText(raw['budget']));
  return {
    q: readText(raw['q']),
    budget: Number.isInteger(budget) && budget >= 1 ? budget : undefined,
  };
}
