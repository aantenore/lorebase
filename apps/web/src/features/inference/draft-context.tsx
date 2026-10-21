import { createContext, use } from 'react';
import type { ProposalDraft } from './use-proposal-draft';

export interface DraftView {
  draft: ProposalDraft;
  /** Confidence from which an element counts as safe; colors the badges. */
  threshold: number;
}

const DraftContext = createContext<DraftView | null>(null);

export const DraftProvider = DraftContext.Provider;

export function useDraftView(): DraftView {
  const view = use(DraftContext);
  if (view === null) throw new Error('useDraftView must be used under a DraftProvider.');
  return view;
}
