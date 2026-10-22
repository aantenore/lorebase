import type { ProposalStatus } from '@/api/types';

export type StatusFilter = ProposalStatus | 'all';

export function isStatusFilter(value: unknown): value is StatusFilter {
  return value === 'open' || value === 'merged' || value === 'closed' || value === 'all';
}

export type ReviewTab = 'proposals' | 'suggestions';
