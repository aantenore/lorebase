/**
 * Schema suggestions: Kinds, Attributes and Relations that Scribe's discovery found in the
 * documents and proposes for the schema, and vocabulary (aliases, Attribute descriptions)
 * consolidated from the words agents asked for and nothing matched. The wire shapes are the
 * generated ones of
 * `./generated/keeper.ts`; `examples` is a free-form object list on the wire, so the evidence
 * is typed here and read defensively by the mapper in `./live/suggestions.ts`.
 */
import type { components } from './generated/keeper';
import type { PageParams } from './types';

type S = components['schemas'];

export type SuggestionType = S['SuggestionOut']['type'];
export type SuggestionStatus = S['SuggestionOut']['status'];
export type SuggestionDecisionValue = S['SuggestionDecisionIn']['decision'];

/** Evidence quoted from a Document. */
export interface DocumentExample {
  document_id: string;
  chunk: number | null;
  /** Offsets into the parsed text of the document. */
  start: number;
  end: number;
  quote: string;
}

/** Evidence of an alias: a phrase agents asked for that matched nothing. There is no Document; told apart by `phrase`. */
export interface MissExample {
  phrase: string;
  misses: number | null;
  callers: number | null;
  similarity: number | null;
}

/** Descriptions carry no evidence of their own: their phrases are the labels. */
export type SuggestionExample = DocumentExample | MissExample;

/** The types of suggestion that propose vocabulary instead of schema elements. */
export const VOCABULARY_TYPES: readonly SuggestionType[] = [
  'kind_alias',
  'attribute_alias',
  'value_alias',
  'attribute_description',
];

export type TypeGuess = S['TypeGuessOut'];
export type SuggestionDuplicate = S['DuplicateOut'];

export type SchemaSuggestion = Omit<
  S['SuggestionOut'],
  'kind' | 'target' | 'type_guess' | 'duplicate_of' | 'examples'
> & {
  kind: string | null;
  target: string | null;
  type_guess: TypeGuess | null;
  duplicate_of: SuggestionDuplicate | null;
  examples: SuggestionExample[];
};

/** Every given filter must match; a list matches any of its values. */
export interface SuggestionFilter {
  status?: SuggestionStatus[];
  type?: SuggestionType[];
  /** Also the pending suggestions below Keeper's minimum support. */
  all?: boolean;
}

export interface SuggestionListParams extends PageParams, SuggestionFilter {}

export interface SuggestionDecisionInput {
  suggestion: string;
  decision: SuggestionDecisionValue;
  /** Api name to use instead of the proposed one (accepted only). */
  name?: string;
}

export interface DecideSuggestionsInput {
  decisions: SuggestionDecisionInput[];
}

export type SuggestionProposal = S['SuggestionProposalOut'];

export interface DecideSuggestionsResult {
  suggestions: SchemaSuggestion[];
  /** The Proposal opened for the accepted suggestions; `null` if none was accepted. */
  proposal: SuggestionProposal | null;
}
