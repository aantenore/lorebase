import { stringify } from 'yaml';
import type { KeeperApi } from './client';
import { MergeConflictsError, NotFoundError, ValidationError, notSupported } from './errors';
import { createFetcher, type Fetcher, type FetcherOptions, type QueryParams } from './fetcher';
import type { components } from './generated/keeper';
import { mapSchema } from './live/schema';
import {
  mapDiff,
  mapHistory,
  mapLineage,
  mapLinks,
  mapNeighborhood,
  mapQuery,
  mapRecordPage,
  mapRecordResponse,
  mapSearch,
  mapTraverse,
} from './live/data';
import {
  mapAttempt,
  mapBranch,
  mapChangeResult,
  mapExecution,
  mapMerge,
  mapResolution,
  mapRevision,
  mapTag,
  mapValidation,
  refusedChange,
  refusedExecution,
} from './live/versioning';
import { splitRecordRef } from './refs';
import type {
  Branch,
  Page,
  PageParams,
  Proposal,
  ProposalDetail,
  ReadScope,
  Resolution,
  SchemaChangeRequest,
} from './types';

export { API_BASE_PATH } from './fetcher';

type S = components['schemas'];

export interface HttpAdapterOptions extends FetcherOptions {
  /** A fetcher to use instead of building one from the options; used by tests. */
  fetcher?: Fetcher;
}

const enc = encodeURIComponent;

/** The fingerprint travels in the path; the body carries the decision only. */
function resolutionBody(resolution: Resolution) {
  const { choice, value, values } = resolution;
  return {
    choice,
    ...(value === undefined ? {} : { value }),
    ...(values === undefined ? {} : { values }),
  };
}

function scopeParams(scope: ReadScope): QueryParams {
  return { branch: scope.branch, at: scope.at };
}

function pageParams(params?: PageParams): QueryParams {
  return { limit: params?.limit, cursor: params?.cursor };
}

/** The schema document as the YAML text Keeper's change-set routes take. */
const documentText = (document: object) => stringify(document, { indent: 2, lineWidth: 0 });

function requireDocument(request: SchemaChangeRequest): object {
  if (request.document === undefined) {
    throw new ValidationError({
      type: 'about:blank',
      title: 'Request validation failed',
      status: 422,
      detail: 'Keeper takes a schema change set as a document; the request carries none.',
      code: 'validation-failed',
    });
  }
  return request.document;
}

/** The adapter that talks to Keeper over `/api/v1`. See `./live/` for the response mappers. */
export function createHttpApi(options: HttpAdapterOptions): KeeperApi {
  const http = options.fetcher ?? createFetcher(options);
  const get = <T>(path: string, query?: QueryParams) =>
    http.json<T>('GET', path, query === undefined ? {} : { query });
  const post = <T>(path: string, body?: unknown, query?: QueryParams) =>
    http.json<T>('POST', path, {
      ...(body === undefined ? {} : { body }),
      ...(query === undefined ? {} : { query }),
    });
  /** A create: fresh `Idempotency-Key`, and a 200 (an existing resource replayed) is a conflict. */
  const create = <T>(path: string, body: unknown) =>
    http.json<T>('POST', path, { body, idempotent: true, created: true });

  const d = (domain: string) => `/domains/${enc(domain)}`;
  const b = (domain: string, branch: string) => `${d(domain)}/branches/${enc(branch)}`;
  const rec = (domain: string, kind: string, key: string) =>
    `${d(domain)}/kinds/${enc(kind)}/records/${enc(key)}`;
  const prop = (domain: string, proposal: string) => `${d(domain)}/proposals/${enc(proposal)}`;

  const branchDetail = async (domain: string, branch: string): Promise<Branch> => {
    try {
      return mapBranch(await get<S['BranchDetail']>(b(domain, branch)));
    } catch (error) {
      if (!(error instanceof NotFoundError)) throw error;
      const tags = await get<S['Page_TagOut_']>(`${d(domain)}/tags`);
      const tag = tags.items.find((t) => t.name === branch);
      if (tag === undefined) throw error;
      return mapTag(tag);
    }
  };

  const explain = async (scope: ReadScope, kind: string, key: string) => {
    if (scope.at !== undefined) {
      throw notSupported('Lineage and history of a Record at a past Revision');
    }
    return await get<S['ExplainOut']>(`${rec(scope.domain, kind, key)}/explain`, {
      branch: scope.branch,
    });
  };

  const revisionLog = (domain: string, branch: string, params?: PageParams) =>
    get<S['LogPage']>(`${b(domain, branch)}/log`, pageParams(params));

  return {
    domains: {
      list: () => get<Page<S['DomainOut']>>('/domains'),
      create: (input) => create('/domains', input),
      get: (domain) => get(d(domain)),
    },

    branches: {
      list: async (domain) => {
        const [branches, tags] = await Promise.all([
          get<S['Page_BranchOut_']>(`${d(domain)}/branches`),
          get<S['Page_TagOut_']>(`${d(domain)}/tags`),
        ]);
        // The listing carries no distance to the default Branch; the detail does.
        const detailed = await Promise.all(
          branches.items.map((branch) => branchDetail(domain, branch.name)),
        );
        return {
          items: [...detailed, ...tags.items.map(mapTag)],
          next_cursor: branches.next_cursor ?? null,
        };
      },
      create: async (domain, input) => {
        const created = await create<S['BranchOut']>(`${d(domain)}/branches`, input);
        return branchDetail(domain, created.name);
      },
      get: (domain, branch) => branchDetail(domain, branch),
      delete: (domain, branch) => http.json('DELETE', b(domain, branch)),
    },

    revisions: {
      log: async (domain, branch, params) => {
        const page = await revisionLog(domain, branch, params);
        return {
          items: page.items.map((revision) => mapRevision(revision, domain)),
          next_cursor: page.next_cursor ?? null,
        };
      },
      get: async (domain, revision) =>
        mapRevision(await get<S['RevisionOut']>(`${d(domain)}/revisions/${enc(revision)}`), domain),
      diff: async (domain, params) =>
        mapDiff(
          await get<S['ChangesPage']>(`${d(domain)}/diff`, {
            from: params.from,
            to: params.to,
            kinds: params.kinds,
            ...pageParams(params),
          }),
        ),
    },

    schema: {
      get: async (scope) =>
        mapSchema(await get<S['SchemaOut']>(`${d(scope.domain)}/schema`, scopeParams(scope))),
      validate: async (scope, request) =>
        mapValidation(
          await post<S['SchemaValidationOut']>(`${d(scope.domain)}/schema:validate`, {
            yaml: documentText(request.document),
            ...(scope.branch === undefined ? {} : { branch: scope.branch }),
            ...(scope.at === undefined ? {} : { at: scope.at }),
          }),
        ),
      applyChanges: async (domain, branch, request) => {
        const body = {
          yaml: documentText(requireDocument(request)),
          ...(request.message === undefined ? {} : { message: request.message }),
          ...(request.dry_run === undefined ? {} : { dry_run: request.dry_run }),
          ...(request.cascade === undefined ? {} : { cascade: request.cascade }),
        };
        try {
          return mapChangeResult(
            await post<S['SchemaChangeOut']>(`${b(domain, branch)}/schema/changes`, body),
          );
        } catch (error) {
          // A change set Keeper refuses is an answer with findings, not a failure.
          if (error instanceof ValidationError && error.issues.length > 0) {
            return refusedChange(error.issues);
          }
          throw error;
        }
      },
    },

    records: {
      list: async (scope, kind, params) =>
        mapRecordPage(
          await get<S['RecordPage']>(`${d(scope.domain)}/kinds/${enc(kind)}/records`, {
            ...scopeParams(scope),
            where: params?.where === undefined ? undefined : JSON.stringify(params.where),
            order_by: params?.order_by?.map((o) => `${o.attr}:${o.dir}`),
            ...pageParams(params),
          }),
        ),
      get: async (scope, kind, key) =>
        mapRecordResponse(
          await get<S['RecordResponse']>(rec(scope.domain, kind, key), scopeParams(scope)),
        ),
      links: async (scope, kind, key, relation, params) =>
        mapLinks(
          await get<S['LinkPage']>(`${rec(scope.domain, kind, key)}/links/${enc(relation)}`, {
            ...scopeParams(scope),
            ...pageParams(params),
          }),
          relation,
        ),
      lineage: async (scope, kind, key) => mapLineage(await explain(scope, kind, key)),
      history: async (scope, kind, key) => mapHistory(await explain(scope, kind, key)),
    },

    query: {
      run: async (scope, query) =>
        mapQuery(
          await post<S['QueryResponse']>(`${d(scope.domain)}/query`, {
            ...query,
            ...scopeParams(scope),
          }),
        ),
      traverse: async (scope, request) => {
        const { kind, key } = splitRecordRef(request.from);
        return mapTraverse(
          await post<S['TraverseResponse']>(`${d(scope.domain)}/traverse`, {
            start: { kind, key },
            path: request.path,
            ...(scope.branch === undefined ? {} : { branch: scope.branch }),
          }),
        );
      },
      neighborhood: async (scope, request) =>
        mapNeighborhood(
          await post<S['NeighborhoodResponse']>(`${d(scope.domain)}/neighborhood`, {
            seeds: request.seeds.map(splitRecordRef),
            depth: request.depth,
            ...(request.relations === undefined ? {} : { relations: request.relations }),
            ...(request.max_nodes === undefined ? {} : { max_nodes: request.max_nodes }),
            ...(scope.branch === undefined ? {} : { branch: scope.branch }),
          }),
        ),
    },

    search: {
      run: async (scope, request) =>
        mapSearch(
          await post<S['SearchResponse']>(`${d(scope.domain)}/search`, {
            ...request,
            ...(scope.branch === undefined ? {} : { branch: scope.branch }),
          }),
        ),
    },

    operations: {
      execute: async (domain, operation, request) => {
        // The key travels as the header; sending it in the body as well would have to match.
        const { idempotency_key: key, ...rest } = request;
        try {
          return mapExecution(
            await http.json<S['ExecutionOut']>(
              'POST',
              `${d(domain)}/operations/${enc(operation)}/execute`,
              {
                body: rest,
                idempotent: key === undefined,
                ...(key === undefined ? {} : { headers: { 'Idempotency-Key': key } }),
              },
            ),
          );
        } catch (error) {
          if (error instanceof ValidationError && error.issues.length > 0) {
            const { edits } = error.problem as { edits?: Record<string, unknown>[] };
            return refusedExecution(edits, error.issues);
          }
          throw error;
        }
      },
    },

    proposals: {
      list: (domain, params) =>
        get<Page<Proposal>>(`${d(domain)}/proposals`, {
          status: params?.status,
          origin: params?.origin,
          ...pageParams(params),
        }),
      create: (domain, input) => create<ProposalDetail>(`${d(domain)}/proposals`, input),
      get: (domain, proposal) => get<ProposalDetail>(prop(domain, proposal)),
      diff: async (domain, proposal, params) => {
        // What the source changed since the two Branches parted, not the distance between heads.
        const detail = await get<ProposalDetail>(prop(domain, proposal));
        const from = detail.attempt?.base ?? detail.target_ref;
        return mapDiff(
          await get<S['ChangesPage']>(`${d(domain)}/diff`, {
            from,
            to: detail.source_ref,
            ...pageParams(params),
          }),
        );
      },
      review: (domain, proposal, input) => post(`${prop(domain, proposal)}/reviews`, input),
      merge: async (domain, proposal) => {
        try {
          return mapMerge(
            await http.json<S['MergeOut']>('POST', `${prop(domain, proposal)}/merge`, {
              idempotent: true,
            }),
          );
        } catch (error) {
          if (error instanceof MergeConflictsError && error.attempt !== undefined) {
            return {
              status: 'conflicts',
              attempt: mapAttempt(error.attempt as unknown as S['ConflictsOut']),
            };
          }
          throw error;
        }
      },
      close: (domain, proposal) => post(`${prop(domain, proposal)}/close`),
      // Keeper has no extraction items yet: every Proposal lists none.
      items: () => Promise.resolve({ items: [], next_cursor: null }),
      decideItems: () => Promise.reject(notSupported('Deciding extraction items')),
    },

    conflicts: {
      list: async (domain, proposal, params) =>
        mapAttempt(
          await get<S['ConflictsOut']>(`${prop(domain, proposal)}/conflicts`, {
            kind: params?.kind,
            status: params?.status,
            ...pageParams(params),
          }),
        ),
      resolve: async (domain, proposal, resolution) =>
        mapResolution(
          await post<S['ResolutionOut']>(
            `${prop(domain, proposal)}/conflicts/${enc(resolution.fingerprint)}/resolution`,
            resolutionBody(resolution),
          ),
        ),
      resolveMany: async (domain, proposal, input) =>
        mapAttempt(
          await post<S['ConflictsOut']>(`${prop(domain, proposal)}/conflicts:resolve`, {
            resolutions: input.resolutions.map((r) => ({
              fingerprint: r.fingerprint,
              ...resolutionBody(r),
            })),
          }),
        ),
    },

    auth: {
      whoami: () => get('/auth/whoami'),
    },

    agents: {
      context: (scope, request) =>
        post(`${d(scope.domain)}/context`, {
          ...(scope.branch === undefined ? {} : { branch: scope.branch }),
          ...request,
        }),
      explain: (scope, kind, key) => explain(scope, kind, key),
    },

    inference: {
      upload: (domain, files) => {
        const form = new FormData();
        for (const file of files) form.append('files', file, file.name);
        return http.json('POST', `${d(domain)}/schema:infer`, { form });
      },
    },

    webhooks: {
      list: async (domain) => (await get<S['WebhookList']>(`${d(domain)}/webhooks`)).items,
      create: (domain, input) => post(`${d(domain)}/webhooks`, input),
      delete: (domain, webhook) => http.json('DELETE', `${d(domain)}/webhooks/${enc(webhook)}`),
    },
  };
}
