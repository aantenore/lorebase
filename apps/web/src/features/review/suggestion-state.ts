import type {
  DecideSuggestionsInput,
  SchemaSuggestion,
  SuggestionDecisionValue,
  SuggestionFilter,
  SuggestionStatus,
  SuggestionType,
} from '@/api/suggestion-types';
import { VOCABULARY_TYPES } from '@/api/suggestion-types';
import { review as reviewMessages } from '@/messages/review';
import type { BadgeTone } from '@/ui/layout';

/** What the filter form holds; `all` stands for "no restriction". */
export interface SuggestionFilterState {
  status: SuggestionStatus | 'all';
  type: SuggestionType | 'all';
  /** Also the suggestions below the minimum support. */
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
  kind: reviewMessages.suggestions.typeOptions.kind,
  attribute: reviewMessages.suggestions.typeOptions.attribute,
  relation: reviewMessages.suggestions.typeOptions.relation,
  kind_alias: reviewMessages.suggestions.typeOptions.kind_alias,
  attribute_alias: reviewMessages.suggestions.typeOptions.attribute_alias,
  value_alias: reviewMessages.suggestions.typeOptions.value_alias,
  attribute_description: reviewMessages.suggestions.typeOptions.attribute_description,
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
  ref.startsWith('+') ? reviewMessages.suggestions.newRef(ref.slice(1)) : ref;

/** Documents a suggestion was observed in; empty when none, as for a vocabulary suggestion. */
export const documentsLabel = (suggestion: SchemaSuggestion): string =>
  suggestion.documents > 0 ? String(suggestion.documents) : '';

/** The Kind that owns an Attribute, the ends of a Relation, `Kind.attribute` of an alias; empty for a Kind. */
export function ownerLabel(suggestion: SchemaSuggestion): string {
  const owner = suggestion.kind === null ? null : refLabel(suggestion.kind);
  if (VOCABULARY_TYPES.includes(suggestion.type)) {
    if (owner === null) return '';
    return suggestion.attribute == null ? owner : `${owner}.${suggestion.attribute}`;
  }
  if (suggestion.type === 'attribute') return owner ?? '';
  if (suggestion.type === 'relation') {
    const unknown = reviewMessages.suggestions.unknownEnd;
    const target = suggestion.target === null ? unknown : refLabel(suggestion.target);
    return reviewMessages.suggestions.ends(owner ?? unknown, target);
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
