/**
 * A small client for the Keeper REST API, for the setup of the run: it creates the Domain,
 * loads the sample and prepares Proposals that the browser flows then work on. It is not the
 * adapter under test, and uses nothing of the app.
 */

// No parameter properties: `scripts/screenshots.ts` imports this file and runs it with Node's
// type stripping, which does not transform them.
export class KeeperError extends Error {
  readonly status: number;
  readonly code: string | undefined;

  constructor(status: number, code: string | undefined, message: string) {
    super(message);
    this.name = 'KeeperError';
    this.status = status;
    this.code = code;
  }
}

export interface Keeper {
  get<T = unknown>(path: string, query?: Record<string, string>): Promise<T>;
  post<T = unknown>(path: string, body?: unknown, headers?: Record<string, string>): Promise<T>;
  patch<T = unknown>(path: string, body: unknown): Promise<T>;
  delete(path: string): Promise<void>;
}

export function createKeeper(baseUrl: string): Keeper {
  const root = `${baseUrl.replace(/\/+$/, '')}/api/v1`;

  async function call<T>(
    method: string,
    path: string,
    init: { body?: unknown; headers?: Record<string, string> } = {},
  ) {
    const response = await fetch(`${root}${path}`, {
      method,
      headers: {
        Accept: 'application/json',
        ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...init.headers,
      },
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    const parsed: unknown = text === '' ? undefined : JSON.parse(text);
    if (!response.ok) {
      const problem = (parsed ?? {}) as {
        code?: string;
        detail?: string;
        title?: string;
        issues?: { path?: unknown[]; message?: string }[];
      };
      const findings = (problem.issues ?? [])
        .slice(0, 5)
        .map((issue) => `\n  ${(issue.path ?? []).join('/')}: ${issue.message ?? ''}`)
        .join('');
      throw new KeeperError(
        response.status,
        problem.code,
        `${method} ${path}: ${String(response.status)} ${problem.detail ?? problem.title ?? text.slice(0, 200)}${findings}`,
      );
    }
    return parsed as T;
  }

  const withQuery = (path: string, query?: Record<string, string>) =>
    query === undefined ? path : `${path}?${new URLSearchParams(query).toString()}`;

  return {
    get: (path, query) => call('GET', withQuery(path, query)),
    post: (path, body, headers) =>
      call('POST', path, {
        ...(body === undefined ? {} : { body }),
        ...(headers === undefined ? {} : { headers }),
      }),
    patch: (path, body) => call('PATCH', path, { body }),
    delete: (path) => call('DELETE', path),
  };
}

/** Polls `/readyz` until Keeper answers ready, or throws after `timeoutMs`. */
export async function waitForKeeper(baseUrl: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let last = 'no answer';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl.replace(/\/+$/, '')}/readyz`);
      if (response.ok) return;
      last = `HTTP ${response.status}`;
    } catch (error) {
      last = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`Keeper at ${baseUrl} was not ready after ${timeoutMs} ms (${last}).`);
}
