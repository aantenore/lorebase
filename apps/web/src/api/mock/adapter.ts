import type { KeeperApi } from '../client';
import { notFound, notSupported, problem, stateError } from '../errors';
import type {
  Branch,
  BranchName,
  Conflict,
  DomainId,
  Page,
  Proposal,
  ProposalDetail,
  ReadScope,
  Revision,
  Schema,
} from '../types';
import { revision as buildRevision } from './builders';
import {
  decodeCursor,
  diffBranches,
  encodeCursor,
  findRecord,
  neighborhood,
  runQuery,
  runSearch,
  toRow,
} from './engine';
import { buildHrDomain } from './sample/hr';
import { buildSupplyDomain } from './sample/supply';
import { headOf, refOf, type BranchData, type DomainData, type MockSeed } from './state';

export interface MockOptions {
  /** Defaults applied when a request omits its own limit. */
  pageSize: number;
  searchLimit: number;
  maxNodes: number;
  /** Seed data; defaults to the sample Domains. */
  seed?: MockSeed;
}

export function buildSampleSeed(): MockSeed {
  return { domains: [buildSupplyDomain(), buildHrDomain()] };
}

const page = <T>(items: T[]): Page<T> => ({ items, next_cursor: null });

function paginate<T>(
  items: T[],
  limit: number | undefined,
  cursor: string | null | undefined,
): Page<T> {
  const offset = decodeCursor(cursor);
  if (limit === undefined) return { items: items.slice(offset), next_cursor: null };
  const next = offset + limit < items.length ? encodeCursor(offset + limit) : null;
  return { items: items.slice(offset, offset + limit), next_cursor: next };
}

/** An in-memory Keeper. Same interface as the HTTP adapter; state lives for the page lifetime. */
export function createMockApi(options: MockOptions): KeeperApi {
  const seed = options.seed ?? buildSampleSeed();
  const domains = new Map<DomainId, DomainData>(seed.domains.map((d) => [d.domain.id, d]));
  let proposalCounter = 100;
  let revisionCounter = 0;

  const domainOf = (id: DomainId): DomainData => {
    const found = domains.get(id);
    if (found === undefined) throw notFound(`Domain ${id}`);
    return found;
  };

  const branchOf = (domain: DomainData, name: BranchName): BranchData => {
    const found = domain.branches.get(name);
    if (found === undefined) throw notFound(`Branch ${name}`);
    return found;
  };

  /** Resolve a branch name or a Branch head Revision id to Branch data. */
  const resolveRef = (domain: DomainData, ref: string): BranchData => {
    const named = domain.branches.get(ref);
    if (named !== undefined) return named;
    for (const data of domain.branches.values()) if (headOf(data) === ref) return data;
    throw notFound(`Ref ${ref}`);
  };

  const scoped = (scope: ReadScope) => {
    const domain = domainOf(scope.domain);
    const data =
      scope.at === undefined
        ? branchOf(domain, scope.branch ?? domain.domain.default_branch)
        : resolveRef(domain, scope.at);
    return { domain, data, revision: headOf(data) };
  };

  const toBranch = (domain: DomainData, name: BranchName, data: BranchData): Branch => {
    const main = domain.branches.get(domain.domain.default_branch);
    const own = new Set(data.log.map((r) => r.id));
    const base = new Set((main?.log ?? []).map((r) => r.id));
    return {
      name,
      head: headOf(data),
      kind: 'branch',
      protected: data.protected,
      behind_default: [...base].filter((id) => !own.has(id)).length,
      ahead_default: [...own].filter((id) => !base.has(id)).length,
    };
  };

  const proposalOf = (domain: DomainData, id: string) => {
    const found = domain.proposals.get(id);
    if (found === undefined) throw notFound(`Proposal ${id}`);
    return found;
  };

  const summary = (detail: ProposalDetail): Proposal => ({
    id: detail.id,
    title: detail.title,
    description: detail.description,
    source_ref: detail.source_ref,
    target_ref: detail.target_ref,
    origin: detail.origin,
    status: detail.status,
    author: detail.author,
    created_at: detail.created_at,
    merged_revision: detail.merged_revision,
    checks: detail.checks,
    policies: detail.policies,
  });

  const nextRevisionId = () => {
    revisionCounter += 1;
    return `e${revisionCounter.toString(16).padStart(11, '0')}`;
  };

  const requireOperation = (data: BranchData, name: string) => {
    const operation = data.schema.operations.find((o) => o.api_name === name);
    if (operation === undefined) throw notFound(`Operation ${name}`);
    return operation;
  };

  return {
    domains: {
      list: () => Promise.resolve(page([...domains.values()].map((d) => d.domain))),
      create: (input) => {
        if (domains.has(input.id))
          return Promise.reject(stateError(`Domain ${input.id} exists.`, 'domain_exists'));
        const defaultBranch = input.default_branch ?? 'main';
        const created: DomainData = {
          domain: {
            id: input.id,
            display_name: input.display_name,
            description: input.description ?? null,
            default_branch: defaultBranch,
            settings: {},
            created_at: new Date().toISOString(),
            created_by: 'usr_mock',
          },
          branches: new Map([
            [
              defaultBranch,
              {
                protected: true,
                schema: {
                  kinds: [],
                  relations: [],
                  traits: [],
                  operations: [],
                  derivations: [],
                  sources: [],
                  search: { modes: ['lexical'], embedding: null },
                },
                records: [],
                links: [],
                log: [
                  buildRevision(
                    input.id,
                    nextRevisionId(),
                    null,
                    'Create Domain',
                    new Date().toISOString(),
                    'import',
                  ),
                ],
              },
            ],
          ]),
          proposals: new Map(),
        };
        domains.set(input.id, created);
        return Promise.resolve(created.domain);
      },
      get: (id) => Promise.resolve(domainOf(id).domain),
    },

    branches: {
      list: (id) => {
        const domain = domainOf(id);
        return Promise.resolve(
          page([...domain.branches].map(([name, data]) => toBranch(domain, name, data))),
        );
      },
      create: (id, input) => {
        const domain = domainOf(id);
        if (domain.branches.has(input.name)) {
          return Promise.reject(stateError(`Branch ${input.name} exists.`, 'branch_exists'));
        }
        const source = resolveRef(domain, input.from ?? domain.domain.default_branch);
        const copy = structuredClone(source);
        copy.protected = false;
        domain.branches.set(input.name, copy);
        return Promise.resolve(toBranch(domain, input.name, copy));
      },
      get: (id, name) => {
        const domain = domainOf(id);
        return Promise.resolve(toBranch(domain, name, branchOf(domain, name)));
      },
      delete: (id, name) => {
        const domain = domainOf(id);
        const data = branchOf(domain, name);
        if (data.protected)
          return Promise.reject(stateError(`Branch ${name} is protected.`, 'branch_protected'));
        domain.branches.delete(name);
        return Promise.resolve();
      },
    },

    revisions: {
      log: (id, name, params) => {
        const data = branchOf(domainOf(id), name);
        return Promise.resolve(paginate<Revision>(data.log, params?.limit, params?.cursor));
      },
      get: (id, rev) => {
        const domain = domainOf(id);
        for (const data of domain.branches.values()) {
          const found = data.log.find((r) => r.id === rev);
          if (found !== undefined) return Promise.resolve(found);
        }
        return Promise.reject(notFound(`Revision ${rev}`));
      },
      diff: (id, params) => {
        const domain = domainOf(id);
        return Promise.resolve(
          diffBranches(
            resolveRef(domain, params.from),
            resolveRef(domain, params.to),
            params,
            params.kinds,
          ),
        );
      },
    },

    schema: {
      get: (scope) => {
        const { data, revision, domain } = scoped(scope);
        const schema: Schema = {
          domain: domain.domain.id,
          branch: scope.branch ?? domain.domain.default_branch,
          revision,
          ...data.schema,
        };
        return Promise.resolve(schema);
      },
      applyChanges: () => Promise.reject(notSupported('Schema changes')),
    },

    records: {
      list: (scope, kind, params) => {
        const { data, revision } = scoped(scope);
        const result = runQuery(
          data,
          { kind, ...params, limit: params?.limit ?? options.pageSize },
          options.pageSize,
        );
        return Promise.resolve({ revision, ...result });
      },
      get: (scope, kind, key) => {
        const { data, revision } = scoped(scope);
        return Promise.resolve({ revision, record: toRow(data, findRecord(data, kind, key)) });
      },
      links: (scope, kind, key, relation) => {
        const { data, revision } = scoped(scope);
        findRecord(data, kind, key);
        const self = refOf(kind, key);
        const forward = data.schema.relations.find((r) => r.api_name === relation);
        const inverse = data.schema.relations.find((r) => r.inverse_api_name === relation);
        if (forward === undefined && inverse === undefined) {
          return Promise.reject(notFound(`Relation ${relation}`));
        }
        const direction = forward === undefined ? 'inverse' : 'forward';
        const links = data.links
          .filter((l) =>
            direction === 'forward'
              ? l.relation === relation && l.from === self
              : l.relation === inverse?.api_name && l.to === self,
          )
          .map((l) => {
            const ref = direction === 'forward' ? l.to : l.from;
            const rec = data.records.find((r) => refOf(r.kind, r.key) === ref);
            return {
              ref,
              title: rec === undefined ? ref : toRow(data, rec).title,
              attributes: l.values,
            };
          });
        return Promise.resolve({ revision, relation, direction, links, next_cursor: null });
      },
      lineage: (scope, kind, key) => {
        const { data, revision } = scoped(scope);
        const rec = findRecord(data, kind, key);
        const origin = data.log[data.log.length - 1];
        const lineage = Object.entries(rec.values).map(([attribute, value]) => ({
          attribute,
          value,
          origin_type: kind === 'Contract' ? ('extraction' as const) : ('source_sync' as const),
          origin:
            kind === 'Contract'
              ? { job: 'job_42', document: 'doc_contracts_oct' }
              : { source: 'erp.vendors', run: 'run_6' },
          actor: kind === 'Contract' ? 'svc_scribe' : 'svc_scribe',
          at: origin?.created_at ?? '',
          revision: origin?.id ?? revision,
        }));
        return Promise.resolve({ revision, lineage });
      },
      history: (scope, kind, key, params) => {
        const { data } = scoped(scope);
        const rec = findRecord(data, kind, key);
        const changed = Object.keys(rec.values).slice(0, 2);
        const entries = data.log.slice(0, params?.limit ?? options.pageSize).map((r) => ({
          revision: r.id,
          at: r.created_at,
          actor: r.author.principal,
          message: r.message,
          changed_attributes: changed,
        }));
        return Promise.resolve({ entries, next_cursor: null });
      },
    },

    query: {
      run: (scope, query) => {
        const { data, revision } = scoped(scope);
        return Promise.resolve({ revision, ...runQuery(data, query, options.pageSize) });
      },
      traverse: () => Promise.reject(notSupported('Traversal')),
      neighborhood: (scope, request) => {
        const { data, revision } = scoped(scope);
        return Promise.resolve({ revision, ...neighborhood(data, request, options.maxNodes) });
      },
    },

    search: {
      run: (scope, request) => {
        const { data, revision } = scoped(scope);
        return Promise.resolve({
          revision,
          results: runSearch(data, request, options.searchLimit),
        });
      },
    },

    operations: {
      execute: (id, name, request) => {
        const domain = domainOf(id);
        const data = branchOf(domain, request.branch ?? domain.domain.default_branch);
        const operation = requireOperation(data, name);
        if (request.dry_run !== true)
          return Promise.reject(notSupported('Committing an Operation'));
        return Promise.resolve({
          status: 'dry_run' as const,
          edits: operation.steps.map((s) => ({ action: s.action })),
          violations: [],
        });
      },
    },

    proposals: {
      list: (id, params) => {
        const items = [...domainOf(id).proposals.values()]
          .map((p) => summary(p.detail))
          .filter((p) => params?.status === undefined || p.status === params.status)
          .filter((p) => params?.origin === undefined || p.origin === params.origin);
        return Promise.resolve(paginate(items, params?.limit, params?.cursor));
      },
      create: (id, input) => {
        const domain = domainOf(id);
        branchOf(domain, input.source_ref);
        branchOf(domain, input.target_ref);
        proposalCounter += 1;
        const detail = {
          id: String(proposalCounter),
          title: input.title,
          description: input.description ?? '',
          source_ref: input.source_ref,
          target_ref: input.target_ref,
          origin: 'manual' as const,
          status: 'open' as const,
          author: 'usr_mock',
          created_at: new Date().toISOString(),
          merged_revision: null,
          checks: [],
          policies: [],
          reviews: [],
        };
        domain.proposals.set(detail.id, { detail, conflicts: [], items: [] });
        return Promise.resolve(summary(detail));
      },
      get: (id, proposal) => Promise.resolve(proposalOf(domainOf(id), proposal).detail),
      diff: (id, proposal) => {
        const domain = domainOf(id);
        const { detail } = proposalOf(domain, proposal);
        const from = domain.branches.get(detail.target_ref);
        const to = domain.branches.get(detail.source_ref);
        if (from === undefined || to === undefined) {
          return Promise.resolve({
            from: detail.target_ref,
            to: detail.source_ref,
            summary: {},
            changes: [],
            next_cursor: null,
          });
        }
        return Promise.resolve(
          diffBranches(from, to, { from: detail.target_ref, to: detail.source_ref }, undefined),
        );
      },
      review: (id, proposal, input) => {
        const state = proposalOf(domainOf(id), proposal);
        const review = {
          reviewer: 'usr_mock',
          verdict: input.verdict,
          comment: input.comment ?? '',
          at: new Date().toISOString(),
        };
        state.detail.reviews.push(review);
        return Promise.resolve(review);
      },
      merge: (id, proposal) => {
        const domain = domainOf(id);
        const state = proposalOf(domain, proposal);
        if (state.detail.status !== 'open') {
          return Promise.reject(
            stateError('Only open Proposals can be merged.', 'proposal_not_open'),
          );
        }
        if (state.conflicts.some((c) => c.resolution === null)) {
          return Promise.reject(
            stateError('Resolve all Conflicts before merging.', 'unresolved_conflicts'),
          );
        }
        const source = branchOf(domain, state.detail.source_ref);
        const target = branchOf(domain, state.detail.target_ref);
        const merged = buildRevision(
          id,
          nextRevisionId(),
          headOf(target),
          `Merge proposal #${proposal}`,
          new Date().toISOString(),
          'merge',
          { originRef: proposal },
        );
        target.schema = structuredClone(source.schema);
        target.records = structuredClone(source.records);
        target.links = structuredClone(source.links);
        target.log = [merged, ...target.log];
        state.detail.status = 'merged';
        state.detail.merged_revision = merged.id;
        return Promise.resolve({ proposal: summary(state.detail), revision: merged.id });
      },
      close: (id, proposal) => {
        const state = proposalOf(domainOf(id), proposal);
        if (state.detail.status !== 'open') {
          return Promise.reject(
            stateError('Only open Proposals can be closed.', 'proposal_not_open'),
          );
        }
        state.detail.status = 'closed';
        return Promise.resolve(summary(state.detail));
      },
      items: (id, proposal, params) =>
        Promise.resolve(
          paginate(proposalOf(domainOf(id), proposal).items, params?.limit, params?.cursor),
        ),
      decideItems: (id, proposal, input) => {
        const state = proposalOf(domainOf(id), proposal);
        const at = new Date().toISOString();
        const touched = state.items.filter((item) => input.item_ids.includes(item.item_id));
        for (const item of touched) {
          item.decision = input.decision;
          item.decided_by = 'usr_mock';
          item.decided_at = at;
        }
        return Promise.resolve(page(touched));
      },
    },

    conflicts: {
      list: (id, proposal) => Promise.resolve(page(proposalOf(domainOf(id), proposal).conflicts)),
      resolve: (id, proposal, conflictId, resolution) => {
        const found = proposalOf(domainOf(id), proposal).conflicts.find((c) => c.id === conflictId);
        if (found === undefined) return Promise.reject(notFound(`Conflict ${conflictId}`));
        if (resolution.choice === 'custom' && resolution.value === undefined) {
          return Promise.reject(
            problem(
              422,
              'Invalid resolution',
              'A custom choice needs a value.',
              'invalid_resolution',
            ),
          );
        }
        found.resolution = resolution;
        found.resolved_by = 'usr_mock';
        found.resolved_at = new Date().toISOString();
        return Promise.resolve(found);
      },
      resolveMany: (id, proposal, input) => {
        const all = proposalOf(domainOf(id), proposal).conflicts;
        const touched: Conflict[] = all.filter((c) => input.conflict_ids.includes(c.id));
        for (const c of touched) {
          c.resolution = input.resolution;
          c.resolved_by = 'usr_mock';
          c.resolved_at = new Date().toISOString();
        }
        return Promise.resolve(page(touched));
      },
    },
  };
}

/**
 * Wrap every adapter method so synchronous failures surface as rejected promises,
 * as with a network call, and optionally settle after `ms` to feel like one.
 */
export function asRemote<T extends object>(target: T, ms: number): T {
  const wrap = (value: unknown): unknown => {
    if (typeof value === 'function') {
      return async (...args: unknown[]) => {
        if (ms > 0) await new Promise((resolve) => setTimeout(resolve, ms));
        return (value as (...a: unknown[]) => unknown)(...args);
      };
    }
    if (typeof value === 'object' && value !== null) {
      return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, wrap(v)]));
    }
    return value;
  };
  return wrap(target) as T;
}
