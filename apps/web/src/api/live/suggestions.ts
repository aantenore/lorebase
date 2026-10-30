import type { components } from '../generated/keeper';
import type {
  DecideSuggestionsInput,
  DecideSuggestionsResult,
  SchemaSuggestion,
  SuggestionExample,
  SuggestionFilter,
} from '../suggestion-types';

type S = components['schemas'];

const num = (value: unknown): number | null => (typeof value === 'number' ? value : null);

function exampleOf(raw: Record<string, unknown>): SuggestionExample | null {
  if (typeof raw['phrase'] === 'string') {
    return {
      phrase: raw['phrase'],
      misses: num(raw['misses']),
      callers: num(raw['callers']),
      similarity: num(raw['similarity']),
    };
  }
  const start = num(raw['start']);
  const end = num(raw['end']);
  if (start === null || end === null) return null;
  return {
    document_id: typeof raw['document_id'] === 'string' ? raw['document_id'] : '',
    chunk: num(raw['chunk']),
    start,
    end,
    quote: typeof raw['quote'] === 'string' ? raw['quote'] : '',
  };
}

export const mapSuggestion = (raw: S['SuggestionOut']): SchemaSuggestion => ({
  ...raw,
  kind: raw.kind ?? null,
  target: raw.target ?? null,
  type_guess: raw.type_guess ?? null,
  duplicate_of: raw.duplicate_of ?? null,
  examples: raw.examples.map(exampleOf).filter((e): e is SuggestionExample => e !== null),
});

export const mapSuggestionsDecided = (raw: S['DecideSuggestionsOut']): DecideSuggestionsResult => ({
  suggestions: raw.suggestions.map(mapSuggestion),
  proposal: raw.proposal ?? null,
});

/** The filters of Keeper's suggestion list, one query parameter per value. */
export function suggestionQuery(filter: SuggestionFilter | undefined) {
  const list = (values: readonly string[] | undefined) =>
    values === undefined || values.length === 0 ? undefined : { repeat: values };
  return {
    status: list(filter?.status),
    type: list(filter?.type),
    all: filter?.all === true ? true : undefined,
  };
}

export const suggestionDecideBody = (input: DecideSuggestionsInput): S['DecideSuggestionsIn'] => ({
  decisions: input.decisions.map((d) => ({
    suggestion: d.suggestion,
    decision: d.decision,
    ...(d.name === undefined ? {} : { name: d.name }),
  })),
});
