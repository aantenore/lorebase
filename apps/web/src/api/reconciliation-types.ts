/**
 * The reconciliation queue (design 10.6): candidates group claims of several Sources that
 * disagree about one value, or two Records that probably describe one entity. The wire shapes
 * are the generated ones of `./generated/keeper.ts`; claims, signals and decisions carry free-form
 * JSON on the wire, so they are typed here and read defensively by `./live/reconciliation.ts`.
 */
import type { components } from './generated/keeper';
import type { AssignedFilter, JsonValue, PageParams } from './types';

type S = components['schemas'];

export type CandidateType = S['CandidateType'];
export type CandidateStatus = S['CandidateStatus'];
export type ReconciliationChoice = S['ReconciliationChoice'];
export type ReconciliationPolicy = S['ReconciliationPolicy'];
export type DecisionStatus = S['ReconciliationDecisionOut']['status'];

/** The passage of a document an extracted claim was read from. */
export interface ClaimEvidence {
  document_id: string;
  chunk: number | null;
  /** Offsets into the parsed text of the document. */
  start: number;
  end: number;
  quote: string;
}

/** One value for one Record and Attribute, and where it comes from. */
export interface ReconciliationClaim {
  claim_id: string;
  value: JsonValue;
  value_hash: string | null;
  /** Source identity: `source:{id}`, `document:{id}`, `principal:{id}`, `import:{batch}`. */
  source: string | null;
  provenance_id: string | null;
  /** The Proposal item holding the claim while it is only proposed. */
  proposal_item: { proposal_id: string; item_id: string } | null;
  evidence: ClaimEvidence | null;
  valid_from: string | null;
  valid_to: string | null;
  /** When the value was written or proposed. */
  at: string | null;
}

export type ReconciliationCandidate = Omit<
  S['ReconciliationCandidateOut'],
  'claims' | 'signals' | 'decision'
> & {
  claims: ReconciliationClaim[];
  /** What the detection stages found: `unit`, `negation`, `similarity`, `equivalent`, ... */
  signals: Record<string, JsonValue>;
  /** The last decision: choice, claim, policy, reason and the claims then. */
  decision: Record<string, JsonValue> | null;
};

/** Every given filter must match. Kind and Attribute are api names; `attribute` needs `kind`. */
export interface CandidateFilter {
  /** The default Branch when absent. */
  branch?: string;
  type?: CandidateType;
  status?: CandidateStatus;
  kind?: string;
  attribute?: string;
  /** Source identity of one of the claims. */
  source?: string;
  /** Only the candidates the caller (`me`), somebody or nobody can decide as steward. */
  assigned?: AssignedFilter;
}

export interface CandidateListParams extends PageParams, CandidateFilter {}

export interface ResolveCandidateInput {
  choice: ReconciliationChoice;
  /** `claim`: the chosen claim. */
  claim_id?: string;
  /** `custom`: the value to write. */
  value?: JsonValue;
  /** `policy`: the policy that picks the claim. */
  policy?: ReconciliationPolicy;
  /** `merge`: key of the Record that stays. */
  survivor?: string;
  /** `merge`: values to set on the survivor, by Attribute api name. */
  values?: Record<string, JsonValue>;
  reason: string;
  /** Answer with what the decision would do; nothing is written. */
  dry_run?: boolean;
}

export type ReconciliationDecision = Omit<
  S['ReconciliationDecisionOut'],
  'candidate' | 'claim_id' | 'revision' | 'proposal' | 'branch' | 'edits' | 'detail'
> & {
  candidate: ReconciliationCandidate;
  claim_id: string | null;
  revision: string | null;
  /** The Proposal a write needing review opened (`proposed`, duplicate merges). */
  proposal: string | null;
  branch: string | null;
  edits: Record<string, JsonValue>[];
  /** Why a policy left the candidate undecided, or what a dry run would do. */
  detail: string | null;
};

/** A bulk decision: a policy or a dismissal over listed ids or over the open ones of a filter. */
export interface ResolveCandidatesInput {
  ids?: string[];
  filter?: Omit<CandidateFilter, 'status'>;
  choice: 'policy' | 'dismiss';
  policy?: ReconciliationPolicy;
  reason: string;
  dry_run?: boolean;
}

export type ResolveCandidatesResult = S['ReconciliationBulkOut'];
export type SweepQueued = S['ReconciliationSweepOut'];
