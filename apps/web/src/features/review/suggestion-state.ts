import type {
  DecideSuggestionsInput,
  SchemaSuggestion,
  SuggestionDecisionValue,
  SuggestionFilter,
  SuggestionStatus,
  SuggestionType,
} from '@/api/suggestion-types';
import type { BadgeTone } from '@/ui/layout';

/** What the filter form holds; `all` stands for "no restriction". */
export interface SuggestionFilterState {
  status: SuggestionStatus | 'all';
  type: SuggestionType | 'all';
  /** Also the pending suggestions below Keeper's minimum support. */
  belowMinSupport: boolean;
}

export const DEFAULT_SUGGESTION_FILTER: SuggestionFilterState = {
  status: 'pending',
  type: 'all',
  belowMinSupport: false,
};

export const STATUS_TONE: Record<SuggestionStatus, BadgeTone> = {
  pending: 'warning',
  accepted: 'success',
  rejected: 'neutral',
};

export const TYPE_LABEL: Record<SuggestionType, string> = {
  kind: 'Kind',
  attribute: 'Attribute',
  relation: 'Relation',
};

export function toSuggestionFilter(state: SuggestionFilterState): SuggestionFilter {
  return {
    ...(state.status === 'all' ? {} : { status: [state.status] }),
    ...(state.type === 'all' ? {} : { type: [state.type] }),
    ...(state.belowMinSupport ? { all: true } : {}),
  };
}

/** A Kind another suggestion proposes is `+Name`; shown as `Name (new)`. */
export const refLabel = (ref: string): string =>
  ref.startsWith('+') ? `${ref.slice(1)} (new)` : ref;

/** The Kind that owns an Attribute, or the ends of a Relation; empty for a Kind. */
export function ownerLabel(suggestion: SchemaSuggestion): string {
  const owner = suggestion.kind === null ? null : refLabel(suggestion.kind);
  if (suggestion.type === 'attribute') return owner ?? '';
  if (suggestion.type === 'relation') {
    const target = suggestion.target === null ? '?' : refLabel(suggestion.target);
    return `${owner ?? '?'} to ${target}`;
  }
  return '';
}

/**
 * The decide body for the selected suggestions. A rename applies to accepted names only, and
 * only when it differs from the proposed name.
 */
export function toDecideInput(
  selected: readonly SchemaSuggestion[],
  decision: SuggestionDecisionValue,
  renames: Readonly<Record<string, string>>,
): DecideSuggestionsInput {
  return {
    decisions: selected.map((suggestion) => {
      const name = renames[suggestion.id]?.trim();
      return {
        suggestion: suggestion.id,
        decision,
        ...(decision === 'accepted' && name !== undefined && name !== '' && name !== suggestion.name
          ? { name }
          : {}),
      };
    }),
  };
}

/** Suggestions that can still be decided: Keeper refuses to accept one twice (409). */
export const isDecidable = (suggestion: SchemaSuggestion): boolean =>
  suggestion.status !== 'accepted';
