import type { components } from '../generated/keeper';
import type {
  CandidateFilter,
  ClaimEvidence,
  ReconciliationCandidate,
  ReconciliationClaim,
  ReconciliationDecision,
  ResolveCandidateInput,
  ResolveCandidatesInput,
} from '../reconciliation-types';
import type { JsonValue } from '../types';

type S = components['schemas'];

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const num = (value: unknown): number | null => (typeof value === 'number' ? value : null);
const isMap = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** Keeper's JSON values are typed `unknown` by the generator; they are JSON on the wire. */
const json = (value: unknown): JsonValue => (value === undefined ? null : (value as JsonValue));

function evidenceOf(raw: unknown): ClaimEvidence | null {
  if (!isMap(raw)) return null;
  const document = text(raw['document_id']);
  const start = num(raw['start']);
  const end = num(raw['end']);
  if (document === null || start === null || end === null) return null;
  return {
    document_id: document,
    chunk: num(raw['chunk']),
    start,
    end,
    quote: text(raw['quote']) ?? '',
  };
}

function proposalItemOf(raw: unknown): ReconciliationClaim['proposal_item'] {
  if (!isMap(raw)) return null;
  const proposal = text(raw['proposal_id']);
  const item = text(raw['item_id']);
  return proposal === null || item === null ? null : { proposal_id: proposal, item_id: item };
}

export const mapClaim = (raw: S['ReconciliationClaimOut']): ReconciliationClaim => ({
  claim_id: raw.claim_id,
  value: json(raw.value),
  value_hash: raw.value_hash ?? null,
  source: raw.source ?? null,
  provenance_id: raw.provenance_id ?? null,
  proposal_item: proposalItemOf(raw.proposal_item),
  evidence: evidenceOf(raw.evidence),
  valid_from: raw.valid_from ?? null,
  valid_to: raw.valid_to ?? null,
  at: raw.at ?? null,
});

const jsonMap = (raw: Record<string, unknown> | null | undefined): Record<string, JsonValue> =>
  Object.fromEntries(Object.entries(raw ?? {}).map(([key, value]) => [key, json(value)]));

export const mapCandidate = (raw: S['ReconciliationCandidateOut']): ReconciliationCandidate => ({
  ...raw,
  claims: raw.claims.map(mapClaim),
  signals: jsonMap(raw.signals),
  decision: raw.decision === null ? null : jsonMap(raw.decision),
});

export const mapDecision = (raw: S['ReconciliationDecisionOut']): ReconciliationDecision => ({
  status: raw.status,
  candidate: mapCandidate(raw.candidate),
  claim_id: raw.claim_id ?? null,
  revision: raw.revision ?? null,
  proposal: raw.proposal ?? null,
  branch: raw.branch ?? null,
  edits: (raw.edits ?? []).map((edit) => jsonMap(edit)),
  detail: raw.detail ?? null,
});

/** The filters of Keeper's candidate list, one query parameter each. */
export const candidateQuery = (filter: CandidateFilter | undefined) => ({
  branch: filter?.branch,
  type: filter?.type,
  status: filter?.status,
  kind: filter?.kind,
  attribute: filter?.attribute,
  source: filter?.source,
});

export const resolveBody = (input: ResolveCandidateInput): S['ReconciliationResolveIn'] => ({
  choice: input.choice,
  ...(input.claim_id === undefined ? {} : { claim_id: input.claim_id }),
  ...(input.value === undefined ? {} : { value: input.value }),
  ...(input.policy === undefined ? {} : { policy: input.policy }),
  ...(input.survivor === undefined ? {} : { survivor: input.survivor }),
  ...(input.values === undefined ? {} : { values: input.values }),
  reason: input.reason,
  dry_run: input.dry_run === true,
});

export const bulkBody = (input: ResolveCandidatesInput): S['ReconciliationBulkIn'] => ({
  ...(input.ids === undefined ? {} : { ids: input.ids }),
  ...(input.filter === undefined ? {} : { filter: input.filter }),
  choice: input.choice,
  ...(input.policy === undefined ? {} : { policy: input.policy }),
  reason: input.reason,
  dry_run: input.dry_run === true,
});
