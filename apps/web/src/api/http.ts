import { stringify } from 'yaml';
import type { KeeperApi } from './client';
import { MergeConflictsError, NotFoundError, ValidationError, notSupported } from './errors';
import { createFetcher, type Fetcher, type FetcherOptions, type QueryParams } from './fetcher';
import type { components } from './generated/keeper';
import { mapSchema } from './live/schema';
import {
  mapDiff,
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
import {
  decideBody,
  itemQuery,
  mapDecided,
  mapDocument,
  mapDocumentDetail,
  mapItem,
} from './live/extraction';
import {
  mapSuggestion,
  mapSuggestionsDecided,
  suggestionDecideBody,
  suggestionQuery,
} from './live/suggestions';
import { splitRecordRef } from './refs';
import type { DocumentText, UploadDocumentInput } from './extraction-types';
import type { FilePreview } from './inference-types';
import type { SourceRun, SourceRunStatus } from './sources-types';
import type {
  Branch,
  HistoryResponse,
  JsonValue,
  LineageResponse,
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

/** The multipart body of `schema:infer`: one `files` part per file, `options` as JSON text. */
function inferForm(
  files: File[],
  options: Record<string, unknown> | undefined,
  recordsPath?: string,
): FormData {
  const form = new FormData();
  for (const file of files) form.append('files', file, file.name);
  if (options !== undefined) form.append('options', JSON.stringify(options));
  if (recordsPath !== undefined && recordsPath !== '') form.append('records_path', recordsPath);
  return form;
}

/** The multipart body of a document upload: `file`, and `extract` and `tiers` when given. */
function documentForm(file: File, input: UploadDocumentInput): FormData {
  const form = new FormData();
  form.append('file', file, file.name);
  if (input.extract !== undefined) form.append('extract', String(input.extract));
  if (input.tiers !== undefined) form.append('tiers', JSON.stringify(input.tiers));
  return form;
}

/** The Scribe sync report a Source run carries in `stats`, as far as the screens read it. */
interface SourceRunStats {
  branch?: string;
  rows?: { read?: number };
  counts?: { added?: number; changed?: number; removed?: number };
}

/** Keeper names the Source by id; the screens use its api name. */
function sourceRun(run: S['SourceRunOut'], apiName: string): SourceRun {
  const stats = run.stats as SourceRunStats;
  const counts = stats.counts ?? {};
  const message = run.error?.['message'];
  return {
    id: run.id,
    source: apiName,
    status: run.status as SourceRunStatus,
    trigger: null,
    started_at: run.started_at ?? null,
    finished_at: run.finished_at ?? null,
    rows_read: stats.rows?.read ?? 0,
    changes: {
      added: counts.added ?? 0,
      updated: counts.changed ?? 0,
      deleted: counts.removed ?? 0,
    },
    branch: stats.branch ?? null,
    proposal: run.proposal_id ?? null,
    error: run.error == null ? null : typeof message === 'string' ? message : 'The run failed.',
  };
}

/** Newest first; runs not started yet (no `started_at`) come first. */
function newestFirst(a: SourceRun, b: SourceRun): number {
  return (b.started_at ?? '\uffff').localeCompare(a.started_at ?? '\uffff');
}

function scopeParams(scope: ReadScope): QueryParams {
  return { branch: scope.branch, at: scope.at };
}

/** The branch and Revision of a scope as members of a request body. */
function scopeBody(scope: ReadScope) {
  return {
    ...(scope.branch === undefined ? {} : { branch: scope.branch }),
    ...(scope.at === undefined ? {} : { at: scope.at }),
  };
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

  const postForm = <T>(path: string, form: FormData) => http.json<T>('POST', path, { form });

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

  /** Lineage, history and explain are served for a Branch head only. */
  const branchOnly = (scope: ReadScope): QueryParams => {
    if (scope.at !== undefined) {
      throw notSupported('Lineage and history of a Record at a past Revision');
    }
    return { branch: scope.branch };
  };

  const explain = async (scope: ReadScope, kind: string, key: string) =>
    get<S['ExplainOut']>(`${rec(scope.domain, kind, key)}/explain`, branchOnly(scope));

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

    derivations: {
      preview: async (scope, request) => {
        const out = await post<S['DerivationPreviewOut']>(
          `${d(scope.domain)}/derivations:preview`,
          {
            yaml: documentText(request.document),
            derivation: request.derivation,
            ...(request.limit === undefined ? {} : { limit: request.limit }),
            ...scopeBody(scope),
          },
        );
        return {
          revision: out.revision,
          kind: out.kind,
          attribute: out.attribute,
          items: out.items.map((item) => ({
            key: item.key,
            title: item.title,
            value: item.value as JsonValue,
            masked: item.masked,
            error: item.error ?? null,
          })),
        };
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
      lineage: async (scope, kind, key) =>
        get<LineageResponse>(`${rec(scope.domain, kind, key)}/lineage`, branchOnly(scope)),
      history: async (scope, kind, key, params) =>
        get<HistoryResponse>(`${rec(scope.domain, kind, key)}/history`, {
          ...branchOnly(scope),
          values: params?.values,
          ...pageParams(params),
        }),
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
      items: async (domain, proposal, params) => {
        const page = await get<{ items: Record<string, unknown>[]; next_cursor?: string | null }>(
          `${prop(domain, proposal)}/items`,
          { ...itemQuery(params), ...pageParams(params) },
        );
        return { items: page.items.map(mapItem), next_cursor: page.next_cursor ?? null };
      },
      decideItems: async (domain, proposal, input) =>
        mapDecided(
          await post<Record<string, unknown>>(
            `${prop(domain, proposal)}/items:decide`,
            decideBody(input),
          ),
        ),
    },

    documents: {
      list: async (domain, params) => {
        const page = await get<S['Page_DocumentOut_']>(
          `${d(domain)}/documents`,
          pageParams(params),
        );
        return { items: page.items.map(mapDocument), next_cursor: page.next_cursor ?? null };
      },
      get: async (domain, document) =>
        mapDocumentDetail(
          await get<S['DocumentDetailOut']>(`${d(domain)}/documents/${enc(document)}`),
        ),
      upload: async (domain, input) => {
        const answer =
          'file' in input
            ? await postForm<S['DocumentUploaded']>(
                `${d(domain)}/documents`,
                documentForm(input.file, input),
              )
            : await post<S['DocumentUploaded']>(`${d(domain)}/documents`, {
                url: input.url,
                ...(input.filename === undefined ? {} : { filename: input.filename }),
                ...(input.extract === undefined ? {} : { extract: input.extract }),
                ...(input.tiers === undefined ? {} : { tiers: input.tiers }),
              });
        return { ...answer, document: mapDocument(answer.document), job_id: answer.job_id ?? null };
      },
      // 404 until Scribe parsed the document.
      text: async (domain, document): Promise<DocumentText | null> => {
        try {
          const found = await get<S['DocumentTextOut']>(
            `${d(domain)}/documents/${enc(document)}/text`,
          );
          return { text: found.text, length: found.length, truncated: found.truncated };
        } catch (error) {
          if (error instanceof NotFoundError) return null;
          throw error;
        }
      },
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

    trace: {
      sessions: async (params) => {
        const page = await get<S['TraceSessionPage']>('/trace/sessions', {
          actor: params?.actor,
          domain_id: params?.domain_id,
          ...pageParams(params),
        });
        return { ...page, next_cursor: page.next_cursor ?? null };
      },
      timeline: (sessionId, params) =>
        get<S['TraceTimeline']>(`/trace/sessions/${enc(sessionId)}`, { actor: params?.actor }),
    },

    webhooks: {
      list: async (domain) => (await get<S['WebhookList']>(`${d(domain)}/webhooks`)).items,
      create: (domain, input) => post(`${d(domain)}/webhooks`, input),
      delete: (domain, webhook) => http.json('DELETE', `${d(domain)}/webhooks/${enc(webhook)}`),
    },

    inference: {
      infer: (domain, req) =>
        'files' in req
          ? postForm(
              `${d(domain)}/schema:infer`,
              inferForm(req.files, req.options, req.recordsPath),
            )
          : post(`${d(domain)}/schema:infer`, {
              postgres: req.postgres,
              ...(req.options === undefined ? {} : { options: req.options }),
            }),
      preview: async (domain, file, options) => {
        const rows = options?.rows === undefined ? '' : `?rows=${String(options.rows)}`;
        const answer = await postForm<{ files: FilePreview[] }>(
          `${d(domain)}/schema:preview${rows}`,
          inferForm([file], undefined, options?.recordsPath),
        );
        const [first] = answer.files;
        if (first === undefined) throw new Error('Keeper returned no preview.');
        return first;
      },
    },

    suggestions: {
      list: async (domain, params) => {
        const page = await get<S['Page_SuggestionOut_']>(`${d(domain)}/schema/suggestions`, {
          ...suggestionQuery(params),
          ...pageParams(params),
        });
        return { items: page.items.map(mapSuggestion), next_cursor: page.next_cursor ?? null };
      },
      decide: async (domain, input) =>
        mapSuggestionsDecided(
          await post<S['DecideSuggestionsOut']>(
            `${d(domain)}/schema/suggestions:decide`,
            suggestionDecideBody(input),
          ),
        ),
    },

    sources: {
      runs: async (domain, params) => {
        const runsOf = async (apiName: string, cursor?: string | null) => {
          const page = await get<S['Page_SourceRunOut_']>(
            `${d(domain)}/sources/${enc(apiName)}/runs`,
            { limit: params?.limit, cursor },
          );
          return {
            items: page.items.map((run) => sourceRun(run, apiName)),
            next_cursor: page.next_cursor ?? null,
          };
        };
        if (params?.source !== undefined) return runsOf(params.source, params.cursor);
        // Keeper lists runs per Source: merge the first page of each Source of the schema.
        const schema = mapSchema(await get<S['SchemaOut']>(`${d(domain)}/schema`));
        const pages = await Promise.all(schema.sources.map((source) => runsOf(source.api_name)));
        const items = pages.flatMap((page) => page.items).sort(newestFirst);
        return {
          items: params?.limit === undefined ? items : items.slice(0, params.limit),
          next_cursor: null,
        };
      },
      sync: (domain, source) => post(`${d(domain)}/sources/${enc(source)}/sync`),
      previewExpressions: async (scope, request) => {
        const out = await post<S['SourcePreviewOut']>(`${d(scope.domain)}/sources:preview`, {
          yaml: documentText(request.document),
          source: request.source,
          rows: request.rows,
          ...scopeBody(scope),
        });
        return {
          attributes: out.attributes,
          rows: out.rows.map((row) =>
            Object.fromEntries(
              Object.entries(row).map(([attribute, cell]) => [
                attribute,
                { value: cell.value as JsonValue, error: cell.error ?? null },
              ]),
            ),
          ),
        };
      },
    },

    admin: {
      apiKeys: {
        list: (domain, params) => get(`${d(domain)}/api-keys`, pageParams(params)),
        create: (domain, input) => post(`${d(domain)}/api-keys`, input),
        revoke: (domain, keyId) => http.json('DELETE', `${d(domain)}/api-keys/${enc(keyId)}`),
      },
      principals: {
        list: (params) => get('/principals', { type: params?.type, ...pageParams(params) }),
      },
      grants: { list: (domain, params) => get(`${d(domain)}/grants`, pageParams(params)) },
      audit: { list: (domain, params) => get(`${d(domain)}/audit`, pageParams(params)) },
    },
  };
}
