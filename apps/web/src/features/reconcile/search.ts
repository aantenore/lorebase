import type { CandidateStatus, CandidateType } from '@/api/reconciliation-types';
import { readText } from '@/app/scope';

/** Tab order of the queue (design 12.3). */
export const CANDIDATE_TYPES: readonly CandidateType[] = [
  'duplicate',
  'contradiction',
  'stale',
  'superseded',
];

export const DEFAULT_TYPE: CandidateType = 'contradiction';

export type StatusFilter = CandidateStatus | 'all';

export const STATUS_FILTERS: readonly StatusFilter[] = [
  'open',
  'resolved',
  'dismissed',
  'obsolete',
  'all',
];

/** The queue's state in the URL; absent members take their defaults. */
export interface ReconcileSearch {
  type?: CandidateType | undefined;
  /** `open` when absent. */
  status?: StatusFilter | undefined;
  kind?: string | undefined;
  attribute?: string | undefined;
  source?: string | undefined;
  signal?: string | undefined;
  /** Id of the candidate on screen. */
  candidate?: string | undefined;
}

const isType = (value: unknown): value is CandidateType =>
  CANDIDATE_TYPES.some((type) => type === value);
const isStatus = (value: unknown): value is StatusFilter =>
  STATUS_FILTERS.some((status) => status === value);

export function validateReconcileSearch(raw: Record<string, unknown>): ReconcileSearch {
  const kind = readText(raw['kind']);
  return {
    type: isType(raw['type']) ? raw['type'] : undefined,
    status: isStatus(raw['status']) ? raw['status'] : undefined,
    kind,
    // Keeper narrows by Attribute within a Kind only.
    attribute: kind === undefined ? undefined : readText(raw['attribute']),
    source: readText(raw['source']),
    signal: readText(raw['signal']),
    candidate: readText(raw['candidate']),
  };
}
