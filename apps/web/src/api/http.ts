import type { KeeperApi } from './client';
import { ApiError } from './errors';
import type { PageParams, ProblemDetails, ReadScope, Resolution } from './types';

/** Path prefix of the Keeper REST API (design 11.1). Protocol constant, not configuration. */
export const API_BASE_PATH = '/api/v1';

type QueryValue = string | number | boolean | string[] | null | undefined;
type QueryParams = Record<string, QueryValue>;

export interface HttpAdapterOptions {
  /** Keeper origin, for example the value of VITE_KEEPER_BASE_URL. */
  baseUrl: string;
  /** Injected for tests; defaults to the global fetch. */
  fetch?: typeof fetch;
  /** Extra headers per request, for example an Authorization header. */
  headers?: () => Record<string, string>;
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

function toSearch(params: QueryParams): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.set(key, Array.isArray(value) ? value.join(',') : String(value));
  }
  const text = search.toString();
  return text === '' ? '' : `?${text}`;
}

function scopeParams(scope: ReadScope): QueryParams {
  return { branch: scope.branch, at: scope.at };
}

function pageParams(params?: PageParams): QueryParams {
  return { limit: params?.limit, cursor: params?.cursor };
}

async function readProblem(response: Response): Promise<ProblemDetails> {
  try {
    const body = (await response.json()) as Partial<ProblemDetails>;
    return {
      type: body.type ?? 'about:blank',
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
      ...(body.detail === undefined ? {} : { detail: body.detail }),
      ...(body.instance === undefined ? {} : { instance: body.instance }),
      ...(body.code === undefined ? {} : { code: body.code }),
    };
  } catch {
    return { type: 'about:blank', title: response.statusText, status: response.status };
  }
}

export function createHttpApi(options: HttpAdapterOptions): KeeperApi {
  const root = `${options.baseUrl.replace(/\/+$/, '')}${API_BASE_PATH}`;
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init));

  async function request<T>(
    method: 'GET' | 'POST' | 'DELETE',
    path: string,
    init: { query?: QueryParams; body?: unknown } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers?.() };
    if (init.body !== undefined) headers['Content-Type'] = 'application/json';

    const response = await doFetch(`${root}${path}${toSearch(init.query ?? {})}`, {
      method,
      headers,
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
    if (!response.ok) throw new ApiError(await readProblem(response));
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  const get = <T>(path: string, query?: QueryParams) =>
    request<T>('GET', path, query === undefined ? {} : { query });
  const post = <T>(path: string, body?: unknown, query?: QueryParams) =>
    request<T>('POST', path, {
      ...(body === undefined ? {} : { body }),
      ...(query === undefined ? {} : { query }),
    });

  const d = (domain: string) => `/domains/${enc(domain)}`;
  const b = (domain: string, branch: string) => `${d(domain)}/branches/${enc(branch)}`;
  const rec = (domain: string, kind: string, key: string) =>
    `${d(domain)}/kinds/${enc(kind)}/records/${enc(key)}`;
  const prop = (domain: string, proposal: string) => `${d(domain)}/proposals/${enc(proposal)}`;

  return {
    domains: {
      list: () => get('/domains'),
      create: (input) => post('/domains', input),
      get: (domain) => get(d(domain)),
    },

    branches: {
      list: (domain) => get(`${d(domain)}/branches`),
      create: (domain, input) => post(`${d(domain)}/branches`, input),
      get: (domain, branch) => get(b(domain, branch)),
      delete: (domain, branch) => request('DELETE', b(domain, branch)),
    },

    revisions: {
      log: (domain, branch, params) => get(`${b(domain, branch)}/log`, pageParams(params)),
      get: (domain, revision) => get(`${d(domain)}/revisions/${enc(revision)}`),
      diff: (domain, params) =>
        get(`${d(domain)}/diff`, {
          from: params.from,
          to: params.to,
          kinds: params.kinds,
          ...pageParams(params),
        }),
    },

    schema: {
      get: (scope) => get(`${d(scope.domain)}/schema`, scopeParams(scope)),
      applyChanges: (domain, branch, req) => post(`${b(domain, branch)}/schema/changes`, req),
    },

    records: {
      list: (scope, kind, params) =>
        get(`${d(scope.domain)}/kinds/${enc(kind)}/records`, {
          ...scopeParams(scope),
          where: params?.where === undefined ? undefined : JSON.stringify(params.where),
          order_by: params?.order_by === undefined ? undefined : JSON.stringify(params.order_by),
          ...pageParams(params),
        }),
      get: (scope, kind, key) => get(rec(scope.domain, kind, key), scopeParams(scope)),
      links: (scope, kind, key, relation, params) =>
        get(`${rec(scope.domain, kind, key)}/links/${enc(relation)}`, {
          ...scopeParams(scope),
          ...pageParams(params),
        }),
      lineage: (scope, kind, key) =>
        get(`${rec(scope.domain, kind, key)}/lineage`, scopeParams(scope)),
      history: (scope, kind, key, params) =>
        get(`${rec(scope.domain, kind, key)}/history`, {
          ...scopeParams(scope),
          ...pageParams(params),
        }),
    },

    query: {
      run: (scope, query) => post(`${d(scope.domain)}/query`, query, scopeParams(scope)),
      traverse: (scope, req) => post(`${d(scope.domain)}/traverse`, req, scopeParams(scope)),
      neighborhood: (scope, req) =>
        post(`${d(scope.domain)}/neighborhood`, req, scopeParams(scope)),
    },

    search: {
      run: (scope, req) => post(`${d(scope.domain)}/search`, req, scopeParams(scope)),
    },

    operations: {
      execute: (domain, operation, req) =>
        post(`${d(domain)}/operations/${enc(operation)}/execute`, req),
    },

    proposals: {
      list: (domain, params) =>
        get(`${d(domain)}/proposals`, {
          status: params?.status,
          origin: params?.origin,
          ...pageParams(params),
        }),
      create: (domain, input) => post(`${d(domain)}/proposals`, input),
      get: (domain, proposal) => get(prop(domain, proposal)),
      diff: (domain, proposal, params) => get(`${prop(domain, proposal)}/diff`, pageParams(params)),
      review: (domain, proposal, input) => post(`${prop(domain, proposal)}/reviews`, input),
      merge: (domain, proposal) => post(`${prop(domain, proposal)}/merge`),
      close: (domain, proposal) => post(`${prop(domain, proposal)}/close`),
      items: (domain, proposal, params) =>
        get(`${prop(domain, proposal)}/items`, pageParams(params)),
      decideItems: (domain, proposal, input) =>
        post(`${prop(domain, proposal)}/items:decide`, input),
    },

    conflicts: {
      list: (domain, proposal) => get(`${prop(domain, proposal)}/conflicts`),
      resolve: (domain, proposal, resolution) =>
        post(
          `${prop(domain, proposal)}/conflicts/${enc(resolution.fingerprint)}/resolution`,
          resolutionBody(resolution),
        ),
      resolveMany: (domain, proposal, input) =>
        post(`${prop(domain, proposal)}/conflicts:resolve`, input),
    },
  };
}
