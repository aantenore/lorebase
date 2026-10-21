import { apiErrorFromProblem, NetworkError } from './errors';
import type { ProblemDetails } from './types';

/** Path prefix of the Keeper REST API (design 11.1). Protocol constant, not configuration. */
export const API_BASE_PATH = '/api/v1';

export type QueryValue = string | number | boolean | string[] | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface FetcherOptions {
  /** Keeper origin without the API path, for example the value of VITE_KEEPER_BASE_URL. */
  baseUrl: string;
  /** Injected for tests; defaults to the global fetch. */
  fetch?: typeof fetch;
  /** Headers added to every request, read per request so a changed token applies at once. */
  headers?: () => Record<string, string>;
  /** Mints the `Idempotency-Key` of a create; defaults to a random UUID. */
  idempotencyKey?: () => string;
  /** How many GET answers are kept for `If-None-Match`; 0 turns the cache off. */
  etagEntries?: number;
}

export interface RequestOptions {
  query?: QueryParams;
  /** JSON body. */
  body?: unknown;
  /** Multipart body (file uploads); the browser sets the boundary. */
  form?: FormData;
  /** Send an `Idempotency-Key`, so retrying the request is safe. */
  idempotent?: boolean;
  /**
   * The request creates a resource, and Keeper answers 201. Keeper answers 200 with the
   * existing resource when a create carries an `Idempotency-Key` and the name is taken by the
   * same caller. The keys of this layer are fresh per call, so that is never a retry of an
   * earlier call of ours: it is reported as `already-exists`.
   */
  created?: boolean;
  headers?: Record<string, string>;
}

export interface Fetched<T> {
  data: T;
  status: number;
  headers: Headers;
  /** The answer was a 304 and `data` is the copy kept from the earlier 200. */
  revalidated: boolean;
}

export interface Fetcher {
  /** The answer with its status and headers. */
  full<T>(method: Method, path: string, options?: RequestOptions): Promise<Fetched<T>>;
  /** The decoded body of the answer (`undefined` for 204). */
  json<T>(method: Method, path: string, options?: RequestOptions): Promise<T>;
  /** Forget the kept GET answers (after the credential changed). */
  clear(): void;
}

export type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE';

const DEFAULT_ETAG_ENTRIES = 256;

export function toSearch(params: QueryParams): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null) continue;
    search.set(key, Array.isArray(value) ? value.join(',') : String(value));
  }
  const text = search.toString();
  return text === '' ? '' : `?${text}`;
}

async function readProblem(response: Response): Promise<ProblemDetails> {
  try {
    const body = (await response.json()) as Partial<ProblemDetails> | null;
    if (body === null || typeof body !== 'object') throw new TypeError('not a problem');
    return {
      ...body,
      type: body.type ?? 'about:blank',
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
    };
  } catch {
    return { type: 'about:blank', title: response.statusText || 'Error', status: response.status };
  }
}

interface Kept {
  etag: string;
  data: unknown;
}

/**
 * The HTTP layer of the live adapter: base URL and `/api/v1`, query strings, JSON bodies,
 * problem details as typed errors, `Authorization` and friends from `headers`, an
 * `Idempotency-Key` on creates, and conditional GETs. Keeper marks every read with an
 * `ETag` bound to the Revision it was served from; the layer keeps the last answer per URL
 * and sends `If-None-Match`, so an unchanged read costs a 304 and no body. Where a browser
 * hides the `ETag` header (cross-origin without exposing it) nothing is kept and every read
 * is a plain 200.
 */
export function createFetcher(options: FetcherOptions): Fetcher {
  const root = `${options.baseUrl.replace(/\/+$/, '')}${API_BASE_PATH}`;
  const doFetch: typeof fetch = options.fetch ?? ((input, init) => fetch(input, init));
  const capacity = options.etagEntries ?? DEFAULT_ETAG_ENTRIES;
  const mintKey = options.idempotencyKey ?? (() => crypto.randomUUID());
  const kept = new Map<string, Kept>();

  const remember = (key: string, entry: Kept) => {
    if (capacity === 0) return;
    kept.delete(key);
    kept.set(key, entry);
    const oldest = kept.keys().next();
    if (kept.size > capacity && oldest.done !== true) kept.delete(oldest.value);
  };

  async function full<T>(
    method: Method,
    path: string,
    init: RequestOptions = {},
  ): Promise<Fetched<T>> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      ...options.headers?.(),
      ...init.headers,
    };
    if (init.body !== undefined) headers['Content-Type'] = 'application/json';
    if (init.idempotent === true) headers['Idempotency-Key'] = mintKey();

    const url = `${root}${path}${toSearch(init.query ?? {})}`;
    const cacheKey = `${headers['Authorization'] ?? ''} ${url}`;
    const previous = method === 'GET' ? kept.get(cacheKey) : undefined;
    if (previous !== undefined) headers['If-None-Match'] = previous.etag;

    let response: Response;
    try {
      response = await doFetch(url, {
        method,
        headers,
        ...(init.form === undefined
          ? init.body === undefined
            ? {}
            : { body: JSON.stringify(init.body) }
          : { body: init.form }),
      });
    } catch (error) {
      throw new NetworkError(error instanceof Error ? error.message : 'The request failed.');
    }

    if (response.status === 304 && previous !== undefined) {
      return {
        data: previous.data as T,
        status: 200,
        headers: response.headers,
        revalidated: true,
      };
    }
    if (!response.ok) throw apiErrorFromProblem(await readProblem(response));
    if (init.created === true && response.status !== 201) {
      throw apiErrorFromProblem({
        type: 'urn:lorebase:problem:already-exists',
        title: 'Already exists',
        status: 409,
        code: 'already-exists',
        detail: 'It exists already: Keeper answered with the existing one instead of creating it.',
      });
    }
    if (response.status === 204) {
      return { data: undefined as T, status: 204, headers: response.headers, revalidated: false };
    }
    const data = (await response.json()) as T;
    const etag = response.headers.get('ETag');
    if (method === 'GET') {
      if (etag === null) kept.delete(cacheKey);
      else remember(cacheKey, { etag, data });
    }
    return { data, status: response.status, headers: response.headers, revalidated: false };
  }

  return {
    full,
    json: async <T>(method: Method, path: string, init?: RequestOptions) =>
      (await full<T>(method, path, init)).data,
    clear: () => {
      kept.clear();
    },
  };
}
