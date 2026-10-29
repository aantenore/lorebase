import type { KeeperApi } from '../client';
import { apiErrorFromProblem, notFound, stateError } from '../errors';
import type {
  CandidateFilter,
  ReconciliationCandidate,
  ReconciliationClaim,
  ReconciliationDecision,
  ReconciliationPolicy,
  ResolveCandidateInput,
  ResolveCandidatesResult,
} from '../reconciliation-types';
import type { JsonValue, ProposalCheck } from '../types';
import { decodeCursor, encodeCursor } from './engine';
import { buildReconciliation } from './sample/reconciliation';
import type { BranchData, StoredRecord } from './state';

const ACTOR = 'usr_mock';

/** Where a decision lands: the Revision of a direct write, or the Proposal of a reviewed one. */
export interface WriteOutcome {
  revision: string | null;
  proposal: string | null;
  branch: string | null;
}

/** What the queue needs from the rest of the mock: Branch data and the write path. */
export interface ReconciliationDeps {
  exists: (domain: string) => boolean;
  defaultBranch: (domain: string) => string;
  branchData: (domain: string, branch: string) => BranchData;
  /**
   * Applies `apply` to `branch` and commits it, or, when the Branch is protected or `review` is
   * set, to a copy named after `reviewBranch` that is then proposed into `branch`.
   */
  commit: (
    domain: string,
    request: {
      branch: string;
      reviewBranch: string;
      review: boolean;
      message: string;
      apply: (data: BranchData) => void;
    },
  ) => Promise<WriteOutcome>;
}

export interface ReconciliationMock {
  api: KeeperApi['reconciliation'];
  /** The `reconciliation` check of a Proposal: its open candidates, `null` when it has none. */
  checkFor: (domain: string, proposal: string) => ProposalCheck | null;
}

function invalid(pointer: string, message: string) {
  return apiErrorFromProblem({
    type: 'urn:lorebase:problem:validation-failed',
    title: 'Request validation failed',
    status: 422,
    code: 'validation-failed',
    detail: message,
    errors: [{ pointer, message, code: 'invalid' }],
  });
}

const notOpen = (candidate: ReconciliationCandidate) =>
  stateError(`Candidate ${candidate.id} is ${candidate.status}, not open.`, 'candidate-not-open');

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Lower ranks win under `authority`: the Attribute's authority Source, Sources, people, documents. */
function rankOf(source: string | null, authority: string | null): number {
  if (source === null) return 9;
  if (authority !== null && source === `source:${authority}`) return 0;
  const order = ['source:', 'principal:', 'document:', 'import:'];
  const index = order.findIndex((prefix) => source.startsWith(prefix));
  return index < 0 ? 8 : index + 1;
}

/** The claims that win under `score` (lower is better); several distinct values mean no decision. */
function best(
  claims: ReconciliationClaim[],
  score: (claim: ReconciliationClaim) => number,
): ReconciliationClaim | null {
  const top = Math.min(...claims.map(score));
  const winners = claims.filter((c) => score(c) === top);
  const first = winners[0];
  if (first === undefined) return null;
  return winners.every((c) => same(c.value, first.value)) ? first : null;
}

function matches(candidate: ReconciliationCandidate, filter: CandidateFilter, branch: string) {
  if (candidate.branch !== branch) return false;
  if (filter.type !== undefined && candidate.type !== filter.type) return false;
  if (filter.status !== undefined && candidate.status !== filter.status) return false;
  if (filter.kind !== undefined && candidate.kind !== filter.kind) return false;
  if (filter.attribute !== undefined && candidate.attribute !== filter.attribute) return false;
  if (filter.source !== undefined && !candidate.claims.some((c) => c.source === filter.source)) {
    return false;
  }
  return true;
}

const newestFirst = (a: ReconciliationCandidate, b: ReconciliationCandidate) =>
  b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id);

/**
 * The reconciliation queue in memory (design 10.6). Decisions write through `deps.commit`: on a
 * protected Branch they open a Proposal and the candidate stays open, linked to it; duplicate
 * merges always go through a Proposal. Nothing is decided without a reason.
 */
export function createReconciliationMock(
  deps: ReconciliationDeps,
  seed: Record<string, ReconciliationCandidate[]> = buildReconciliation(),
): ReconciliationMock {
  const store = new Map(Object.entries(seed));
  let jobs = 0;

  const listOf = (domain: string): ReconciliationCandidate[] => {
    if (!deps.exists(domain)) throw notFound(`Domain ${domain}`);
    let found = store.get(domain);
    if (found === undefined) {
      found = [];
      store.set(domain, found);
    }
    return found;
  };

  const find = (domain: string, id: string) => {
    const found = listOf(domain).find((c) => c.id === id);
    if (found === undefined) throw notFound(`Candidate ${id}`);
    return found;
  };

  const recordOf = (domain: string, candidate: ReconciliationCandidate, key: string) =>
    deps
      .branchData(domain, candidate.branch)
      .records.find((r) => r.kind === candidate.kind && r.key === key);

  const currentOf = (domain: string, candidate: ReconciliationCandidate): unknown => {
    const key = candidate.keys[0];
    if (key === undefined || candidate.attribute === null) return undefined;
    return recordOf(domain, candidate, key)?.values[candidate.attribute];
  };

  const filtered = (domain: string, filter: CandidateFilter) => {
    if (filter.attribute !== undefined && filter.kind === undefined) {
      throw invalid('/query/attribute', 'needs kind');
    }
    const branch = filter.branch ?? deps.defaultBranch(domain);
    return listOf(domain)
      .filter((c) => matches(c, filter, branch))
      .sort(newestFirst);
  };

  const requireReason = (reason: string) => {
    if (reason.trim() === '') throw invalid('/body/reason', 'A reason is required.');
  };

  const decide = (
    candidate: ReconciliationCandidate,
    status: ReconciliationCandidate['status'],
    decision: Record<string, JsonValue>,
  ) => {
    const now = new Date().toISOString();
    candidate.status = status;
    candidate.decision = {
      ...decision,
      claims: structuredClone(candidate.claims) as unknown as JsonValue,
    };
    candidate.decided_by = ACTOR;
    candidate.decided_at = now;
    candidate.updated_at = now;
  };

  const outcome = (
    candidate: ReconciliationCandidate,
    status: ReconciliationDecision['status'],
    extra: Partial<ReconciliationDecision> = {},
  ): ReconciliationDecision => ({
    status,
    candidate: structuredClone(candidate),
    claim_id: null,
    revision: null,
    proposal: null,
    branch: null,
    edits: [],
    detail: null,
    ...extra,
  });

  const policyPick = (
    domain: string,
    candidate: ReconciliationCandidate,
    policy: ReconciliationPolicy,
  ): ReconciliationClaim | null => {
    const { claims } = candidate;
    if (policy === 'authority') {
      const kind = deps
        .branchData(domain, candidate.branch)
        .schema.kinds.find((k) => k.api_name === candidate.kind);
      const attribute = Object.values(kind?.attributes ?? {}).find(
        (a) => a.api_name === candidate.attribute,
      );
      const authority = attribute?.authority ?? null;
      return best(claims, (c) => rankOf(c.source, authority));
    }
    if (policy === 'newest_wins') {
      return best(claims, (c) => -Date.parse(c.valid_from ?? c.at ?? '1970-01-01'));
    }
    if (claims.length !== 2) {
      throw invalid('/body/policy', `${policy} needs a candidate with two claims.`);
    }
    const current = currentOf(domain, candidate);
    const own = claims.find((c) => same(c.value, current));
    const other = claims.find((c) => !same(c.value, current));
    return (policy === 'prefer_current' ? own : other) ?? null;
  };

  const merge = async (
    domain: string,
    candidate: ReconciliationCandidate,
    input: ResolveCandidateInput,
  ): Promise<ReconciliationDecision> => {
    const survivor = input.survivor;
    const absorbed = candidate.keys.find((k) => k !== survivor);
    if (survivor === undefined || !candidate.keys.includes(survivor) || absorbed === undefined) {
      throw invalid('/body/survivor', 'Pick one of the two Records as the survivor.');
    }
    const values = input.values ?? {};
    const kind = candidate.kind ?? '';
    const edits: Record<string, JsonValue>[] = [
      { action: 'update', kind, key: survivor, values },
      { action: 'relink', kind, from: absorbed, to: survivor },
      { action: 'delete', kind, key: absorbed },
    ];
    if (input.dry_run === true) return outcome(candidate, 'dry_run', { edits });
    const written = await deps.commit(domain, {
      branch: candidate.branch,
      reviewBranch: `reconcile/${candidate.id}`,
      review: true,
      message: `Merge ${kind} ${absorbed} into ${survivor}`,
      apply: (data) => {
        const keep = data.records.find((r) => r.kind === kind && r.key === survivor);
        if (keep !== undefined) keep.values = { ...keep.values, ...values };
        data.records = data.records.filter((r) => !(r.kind === kind && r.key === absorbed));
        const from = `${kind}/${absorbed}`;
        const to = `${kind}/${survivor}`;
        for (const link of data.links) {
          if (link.from === from) link.from = to;
          if (link.to === from) link.to = to;
        }
      },
    });
    candidate.proposal_id = written.proposal;
    candidate.decision = {
      choice: 'merge',
      survivor,
      values: structuredClone(values),
      reason: input.reason,
    };
    candidate.updated_at = new Date().toISOString();
    return outcome(candidate, 'proposed', { ...written, edits });
  };

  const resolve = async (
    domain: string,
    id: string,
    input: ResolveCandidateInput,
  ): Promise<ReconciliationDecision> => {
    const candidate = find(domain, id);
    if (candidate.status !== 'open') throw notOpen(candidate);
    requireReason(input.reason);
    if (candidate.type === 'duplicate') {
      if (input.choice !== 'merge') throw invalid('/body/choice', 'Expected merge.');
      return merge(domain, candidate, input);
    }
    if (input.choice === 'merge') {
      throw invalid('/body/choice', 'Expected claim, keep_current, custom or policy.');
    }
    const current = currentOf(domain, candidate);
    const record = { choice: input.choice, reason: input.reason } as Record<string, JsonValue>;

    let value: JsonValue | undefined;
    let chosen: ReconciliationClaim | null = null;
    if (input.choice === 'claim') {
      chosen = candidate.claims.find((c) => c.claim_id === input.claim_id) ?? null;
      if (chosen === null) throw invalid('/body/claim_id', 'No such claim on this candidate.');
      value = chosen.value;
    } else if (input.choice === 'custom') {
      if (input.value === undefined) throw invalid('/body/value', 'A value is required.');
      value = input.value;
    } else if (input.choice === 'policy') {
      if (input.policy === undefined) throw invalid('/body/policy', 'A policy is required.');
      record['policy'] = input.policy;
      chosen = policyPick(domain, candidate, input.policy);
      if (chosen === null) {
        return outcome(candidate, 'undecided', {
          detail: `The ${input.policy} policy ranks the claims equally; pick one.`,
        });
      }
      value = chosen.value;
    }
    if (chosen !== null) record['claim_id'] = chosen.claim_id;
    if (input.choice === 'custom') record['value'] = value ?? null;

    const writes = value !== undefined && !same(value, current);
    const edits: Record<string, JsonValue>[] = writes
      ? [
          {
            action: 'update',
            kind: candidate.kind,
            key: candidate.keys[0] ?? '',
            attribute: candidate.attribute,
            value: value ?? null,
          },
        ]
      : [];
    const claimId = chosen?.claim_id ?? null;
    if (input.dry_run === true) {
      return outcome(candidate, 'dry_run', {
        claim_id: claimId,
        edits,
        detail: writes ? null : 'The current value stays; nothing is written.',
      });
    }
    if (!writes) {
      decide(candidate, 'resolved', record);
      return outcome(candidate, 'resolved', { claim_id: claimId });
    }
    const written = await deps.commit(domain, {
      branch: candidate.branch,
      reviewBranch: `reconcile/${candidate.id}`,
      review: false,
      message: `Reconcile ${candidate.kind ?? ''} ${candidate.keys.join(', ')} ${candidate.attribute ?? ''}`,
      apply: (data) => {
        const target: StoredRecord | undefined = data.records.find(
          (r) => r.kind === candidate.kind && r.key === candidate.keys[0],
        );
        if (target !== undefined && candidate.attribute !== null) {
          target.values = { ...target.values, [candidate.attribute]: value };
        }
      },
    });
    if (written.proposal !== null) {
      candidate.proposal_id = written.proposal;
      candidate.decision = record;
      candidate.updated_at = new Date().toISOString();
      return outcome(candidate, 'proposed', { ...written, claim_id: claimId, edits });
    }
    decide(candidate, 'resolved', record);
    return outcome(candidate, 'resolved', { ...written, claim_id: claimId, edits });
  };

  const dismiss = (domain: string, id: string, reason: string) => {
    const candidate = find(domain, id);
    if (candidate.status !== 'open') throw notOpen(candidate);
    requireReason(reason);
    decide(candidate, 'dismissed', { action: 'dismiss', reason });
    return outcome(candidate, 'dismissed');
  };

  return {
    api: {
      list: (domain, params) => {
        const all = filtered(domain, params ?? {});
        const offset = decodeCursor(params?.cursor);
        const limit = params?.limit;
        const items = limit === undefined ? all.slice(offset) : all.slice(offset, offset + limit);
        const next =
          limit !== undefined && offset + limit < all.length ? encodeCursor(offset + limit) : null;
        return Promise.resolve({ items: structuredClone(items), next_cursor: next });
      },
      get: (domain, id) => Promise.resolve(structuredClone(find(domain, id))),
      resolve,
      dismiss: (domain, id, reason) => Promise.resolve(dismiss(domain, id, reason)),
      reopen: (domain, id, reason) => {
        const candidate = find(domain, id);
        if (candidate.status !== 'dismissed') {
          return Promise.reject(
            stateError(
              `Candidate ${id} is ${candidate.status}, not dismissed.`,
              'candidate-not-dismissed',
            ),
          );
        }
        requireReason(reason);
        decide(candidate, 'open', { action: 'reopen', reason });
        candidate.decided_by = null;
        candidate.decided_at = null;
        return Promise.resolve(outcome(candidate, 'reopened'));
      },
      resolveMany: async (domain, input): Promise<ResolveCandidatesResult> => {
        if ((input.ids === undefined) === (input.filter === undefined)) {
          throw invalid('/body', 'give either ids or filter');
        }
        if (input.choice === 'policy' && input.policy === undefined) {
          throw invalid('/body/policy', 'required for the choice policy');
        }
        requireReason(input.reason);
        const ids =
          input.ids ?? filtered(domain, { ...input.filter, status: 'open' }).map((c) => c.id);
        const items: ResolveCandidatesResult['items'] = [];
        for (const id of [...new Set(ids)]) {
          try {
            const decided =
              input.choice === 'dismiss'
                ? input.dry_run === true
                  ? outcome(find(domain, id), 'dry_run')
                  : dismiss(domain, id, input.reason)
                : await resolve(domain, id, {
                    choice: 'policy',
                    reason: input.reason,
                    ...(input.policy === undefined ? {} : { policy: input.policy }),
                    ...(input.dry_run === undefined ? {} : { dry_run: input.dry_run }),
                  });
            items.push({
              candidate: id,
              status: decided.status,
              detail: decided.detail,
              proposal: decided.proposal,
              revision: decided.revision,
            });
          } catch (error) {
            items.push({
              candidate: id,
              status: 'failed',
              detail: error instanceof Error ? error.message : String(error),
            });
          }
        }
        const counts: Record<string, number> = {};
        for (const item of items) counts[item.status] = (counts[item.status] ?? 0) + 1;
        return { count: items.length, counts, items };
      },
      sweep: (domain, branch) => {
        listOf(domain);
        jobs += 1;
        return Promise.resolve({ job_id: jobs, branch: branch ?? deps.defaultBranch(domain) });
      },
    },
    checkFor: (domain, proposal) => {
      const open = (store.get(domain) ?? [])
        .filter((c) => c.status === 'open' && c.proposal_id === proposal)
        .sort(newestFirst);
      if (open.length === 0) return null;
      return {
        name: 'reconciliation',
        status: 'pending',
        message: `${open.length.toString()} open reconciliation ${open.length === 1 ? 'candidate' : 'candidates'}.`,
        candidates: open.map((c) => c.id),
      };
    },
  };
}
