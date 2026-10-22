import type { SearchMode, SearchRequest, SearchTuning } from '@/api/types';

/**
 * One parameter set of the Retrieval lab, as typed: empty text means "not set", so the Domain's
 * configured value applies on the Keeper (`tuning`) or the app default does (`limit`).
 */
export interface LabParams {
  mode: SearchMode;
  lexicalWeight: string;
  semanticWeight: string;
  rrfK: string;
  trigramThreshold: string;
  trigramWeight: string;
  typoCandidates: string;
  candidates: string;
  limit: string;
  kinds: string[];
}

export const MODES: SearchMode[] = ['lexical', 'semantic', 'hybrid'];

/** The knobs of `tuning`, with the parameter that carries each one. */
const TUNING_FIELDS = [
  ['lexical_weight', 'lexicalWeight', false],
  ['semantic_weight', 'semanticWeight', false],
  ['rrf_k', 'rrfK', true],
  ['trigram_threshold', 'trigramThreshold', false],
  ['trigram_weight', 'trigramWeight', false],
  ['typo_candidates', 'typoCandidates', true],
  ['candidates', 'candidates', true],
] as const satisfies readonly (readonly [keyof SearchTuning, keyof LabParams, boolean])[];

export function defaultParams(): LabParams {
  return {
    mode: 'lexical',
    lexicalWeight: '',
    semanticWeight: '',
    rrfK: '',
    trigramThreshold: '',
    trigramWeight: '',
    typoCandidates: '',
    candidates: '',
    limit: '',
    kinds: [],
  };
}

/** A number from typed text: `undefined` when empty, `null` when it is not a number. */
function readNumber(text: string, integer: boolean): number | undefined | null {
  if (text.trim() === '') return undefined;
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0) return null;
  return integer && !Number.isInteger(value) ? null : value;
}

export interface ParamsRequest {
  request: SearchRequest;
  /** Parameters whose text is not usable, by name; the request leaves them out. */
  errors: Partial<Record<keyof LabParams, string>>;
}

/**
 * The search request of a parameter set. A query vector is sent only in the modes that use it;
 * `limit` falls back to `defaultLimit`.
 */
export function toRequest(
  params: LabParams,
  question: string,
  vector: number[] | null,
  defaultLimit: number,
): ParamsRequest {
  const errors: ParamsRequest['errors'] = {};
  const tuning: SearchTuning = {};
  for (const [name, field, integer] of TUNING_FIELDS) {
    const value = readNumber(params[field], integer);
    if (value === null)
      errors[field] = integer ? 'A whole number, 0 or more' : 'A number, 0 or more';
    else if (value !== undefined) tuning[name] = value;
  }
  const limit = readNumber(params.limit, true);
  if (limit === null) errors.limit = 'A whole number, 0 or more';

  const request: SearchRequest = {
    q: question,
    mode: params.mode,
    limit: limit === undefined || limit === null || limit === 0 ? defaultLimit : limit,
    ...(params.kinds.length > 0 ? { kinds: params.kinds } : {}),
    ...(Object.keys(tuning).length > 0 ? { tuning } : {}),
    ...(params.mode !== 'lexical' && vector !== null ? { query_vector: vector } : {}),
  };
  return { request, errors };
}

/** Whether a mode needs a query vector. */
export const needsVector = (mode: SearchMode) => mode !== 'lexical';
