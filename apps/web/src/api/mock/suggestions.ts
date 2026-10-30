import type { KeeperApi } from '../client';
import { apiErrorFromProblem, notFound } from '../errors';
import type {
  DecideSuggestionsInput,
  DecideSuggestionsResult,
  SchemaSuggestion,
  SuggestionFilter,
  SuggestionProposal,
} from '../suggestion-types';
import type { Proposal } from '../types';
import { decodeCursor, encodeCursor } from './engine';
import { buildSuggestions, MOCK_MIN_SUPPORT } from './sample/suggestions';

export interface SuggestionsMock {
  api: KeeperApi['suggestions'];
}

const ACTOR = 'usr_mock';

/** Aliases and descriptions change the Kind or Attribute they belong to. */
const SECTION = {
  kind: 'kinds',
  attribute: 'attributes',
  relation: 'relations',
  kind_alias: 'kinds',
  attribute_alias: 'attributes',
  value_alias: 'attributes',
  attribute_description: 'attributes',
} as const;
const PREFIX = { kinds: 'knd', attributes: 'att', relations: 'rel' } as const;

/** Whether a suggestion passes the list filters; pending ones under the support need `all`. */
export function matches(suggestion: SchemaSuggestion, filter: SuggestionFilter | undefined) {
  if (filter?.status !== undefined && !filter.status.includes(suggestion.status)) return false;
  if (filter?.type !== undefined && !filter.type.includes(suggestion.type)) return false;
  return (
    filter?.all === true ||
    suggestion.status !== 'pending' ||
    suggestion.support >= MOCK_MIN_SUPPORT
  );
}

function invalid(pointer: string, message: string) {
  return apiErrorFromProblem({
    type: 'urn:lorebase:problem:validation-failed',
    title: 'Request validation failed',
    status: 422,
    code: 'validation-failed',
    detail: 'The accepted suggestions do not make a valid schema change set.',
    errors: [{ pointer, message, code: 'invalid' }],
  });
}

/**
 * Schema suggestions in memory. Accepting turns the accepted suggestions of one request into one
 * change set on a new Branch, proposed through `openProposal`; a refusal changes nothing.
 */
export function createSuggestionsMock(
  openProposal: (domain: string, title: string) => Promise<Pick<Proposal, 'id' | 'source_ref'>>,
  seed: Record<string, SchemaSuggestion[]> = buildSuggestions(),
): SuggestionsMock {
  const store = new Map(Object.entries(seed));
  const listOf = (domain: string): SchemaSuggestion[] => {
    const found = store.get(domain);
    if (found === undefined) throw notFound(`Domain ${domain}`);
    return found;
  };

  const decide = async (
    domain: string,
    input: DecideSuggestionsInput,
  ): Promise<DecideSuggestionsResult> => {
    const all = listOf(domain);
    const targets = input.decisions.map((decision, index) => {
      const found = all.find((s) => s.id === decision.suggestion);
      if (found === undefined) throw notFound(`Suggestion ${decision.suggestion}`);
      if (found.status === 'accepted') {
        throw apiErrorFromProblem({
          type: 'urn:lorebase:problem:conflict',
          title: 'Already accepted',
          status: 409,
          detail: `Suggestion ${found.name} was already accepted.`,
        });
      }
      const name = decision.decision === 'accepted' ? (decision.name ?? found.name) : found.name;
      return { found, decision, name, pointer: `/decisions/${index.toString()}` };
    });

    const accepted = targets.filter((t) => t.decision.decision === 'accepted');
    const newKinds = new Set(
      accepted.filter((t) => t.found.type === 'kind').map((t) => `+${t.found.name}`),
    );
    for (const t of accepted) {
      for (const ref of [t.found.kind, t.found.target]) {
        if (ref?.startsWith('+') === true && !newKinds.has(ref)) {
          throw invalid(
            `${t.pointer}/suggestion`,
            `${t.name} refers to the new Kind ${ref.slice(1)}, which is not accepted in this request.`,
          );
        }
      }
    }

    let proposal: SuggestionProposal | null = null;
    if (accepted.length > 0) {
      const title = `Schema suggestions: ${accepted.map((t) => t.name).join(', ')}`;
      const opened = await openProposal(domain, title);
      proposal = {
        id: opened.id,
        title,
        branch: opened.source_ref,
        changes: accepted.map((t) => ({
          op: 'create' as const,
          section: SECTION[t.found.type],
          id: `${PREFIX[SECTION[t.found.type]]}_${t.found.id.slice(-8)}`,
          api_name: t.name,
        })),
      };
    }

    const now = new Date().toISOString();
    for (const { found, decision, name } of targets) {
      const isAccepted = decision.decision === 'accepted';
      found.status = decision.decision;
      found.name = name;
      found.decided_by = decision.decision === 'pending' ? null : ACTOR;
      found.decided_at = decision.decision === 'pending' ? null : now;
      found.updated_at = now;
      found.proposal_id = isAccepted ? (proposal?.id ?? null) : null;
      found.branch = isAccepted ? (proposal?.branch ?? null) : null;
    }
    return { suggestions: structuredClone(targets.map((t) => t.found)), proposal };
  };

  return {
    api: {
      list: (domain, params) => {
        const all = listOf(domain).filter((s) => matches(s, params));
        const offset = decodeCursor(params?.cursor);
        const limit = params?.limit;
        const items = limit === undefined ? all.slice(offset) : all.slice(offset, offset + limit);
        const next =
          limit !== undefined && offset + limit < all.length ? encodeCursor(offset + limit) : null;
        return Promise.resolve({ items: structuredClone(items), next_cursor: next });
      },
      decide,
    },
  };
}
