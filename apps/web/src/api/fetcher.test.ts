import { urlOf } from '@/test/fetch';
import {
  ApprovalsRequiredError,
  ConflictError,
  ForbiddenError,
  MergeConflictsError,
  NetworkError,
  NotFoundError,
  ProtectedBranchError,
  UnauthorizedError,
  ValidationError,
  describeError,
} from './errors';
import { CLIENT_HEADER, CLIENT_NAME, createFetcher } from './fetcher';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: BodyInit | null | undefined;
}

function scripted(...responses: (Response | Error)[]) {
  const calls: Call[] = [];
  const queue = [...responses];
  const fetchStub = ((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: urlOf(input),
      method: init?.method ?? 'GET',
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body,
    });
    const next = queue.shift() ?? new Response('{}', { status: 200 });
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  }) as typeof fetch;
  return { calls, fetchStub };
}

const json = (body: unknown, init: ResponseInit = {}) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json', ...init.headers },
    ...init,
  });

const problem = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  new Response(
    JSON.stringify({ type: `urn:lorebase:problem:${code}`, title: 'T', status, code, ...extra }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );

describe('fetcher', () => {
  it('prefixes the API path, encodes the query and drops empty values', async () => {
    const { calls, fetchStub } = scripted(json({ ok: true }));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test/', fetch: fetchStub });
    await fetcher.json('GET', '/domains', {
      query: { limit: 5, cursor: null, kinds: ['A', 'B'], branch: undefined, flag: false },
    });
    expect(calls[0]?.url).toBe('https://keeper.test/api/v1/domains?limit=5&kinds=A%2CB&flag=false');
  });

  it('sends the headers of the moment, the JSON body and an Idempotency-Key on creates', async () => {
    const { calls, fetchStub } = scripted(json({}), json({}));
    let token = 'one';
    const fetcher = createFetcher({
      baseUrl: 'https://keeper.test',
      fetch: fetchStub,
      headers: () => ({ Authorization: `Bearer ${token}` }),
      idempotencyKey: (() => {
        let n = 0;
        return () => `key-${++n}`;
      })(),
    });
    await fetcher.json('POST', '/domains', { body: { id: 'x' }, idempotent: true });
    token = 'two';
    await fetcher.json('POST', '/domains/x/proposals', { body: { title: 't' } });
    expect(calls[0]?.headers).toMatchObject({
      Authorization: 'Bearer one',
      'Content-Type': 'application/json',
      'Idempotency-Key': 'key-1',
    });
    expect(calls[0]?.body).toBe('{"id":"x"}');
    expect(calls[1]?.headers['Authorization']).toBe('Bearer two');
    expect(calls[1]?.headers['Idempotency-Key']).toBeUndefined();
  });

  it('marks every request as the web app, so agent traces leave it out', async () => {
    const { calls, fetchStub } = scripted(json({}), json({}));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    await fetcher.json('GET', '/domains');
    await fetcher.json('POST', '/domains/x/query', { body: { kind: 'Part' } });
    expect(calls.map((call) => call.headers[CLIENT_HEADER])).toEqual([CLIENT_NAME, CLIENT_NAME]);
    expect(CLIENT_HEADER).toBe('X-Lorebase-Client');
    expect(CLIENT_NAME).toBe('web');
  });

  it('revalidates a read with If-None-Match and serves the kept body on 304', async () => {
    const { calls, fetchStub } = scripted(
      json({ n: 1 }, { headers: { ETag: '"r1"' } }),
      new Response(null, { status: 304, headers: { ETag: '"r1"' } }),
      json({ n: 2 }, { headers: { ETag: '"r2"' } }),
    );
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    expect(await fetcher.json('GET', '/domains/a')).toEqual({ n: 1 });
    expect(calls[0]?.headers['If-None-Match']).toBeUndefined();
    const second = await fetcher.full('GET', '/domains/a');
    expect(second.data).toEqual({ n: 1 });
    expect(second.revalidated).toBe(true);
    expect(calls[1]?.headers['If-None-Match']).toBe('"r1"');
    expect(await fetcher.json('GET', '/domains/a')).toEqual({ n: 2 });
    expect(calls[2]?.headers['If-None-Match']).toBe('"r1"');
  });

  it('keeps answers apart per credential and never revalidates a write', async () => {
    const { calls, fetchStub } = scripted(
      json({ who: 'a' }, { headers: { ETag: '"x"' } }),
      json({ who: 'b' }, { headers: { ETag: '"y"' } }),
      json({}),
    );
    let token = 'a';
    const fetcher = createFetcher({
      baseUrl: 'https://keeper.test',
      fetch: fetchStub,
      headers: () => ({ Authorization: `Bearer ${token}` }),
    });
    await fetcher.json('GET', '/auth/whoami');
    token = 'b';
    await fetcher.json('GET', '/auth/whoami');
    expect(calls[1]?.headers['If-None-Match']).toBeUndefined();
    await fetcher.json('POST', '/domains/a/query', { body: {} });
    expect(calls[2]?.headers['If-None-Match']).toBeUndefined();
  });

  it('does not keep answers when the browser hides the ETag', async () => {
    const { calls, fetchStub } = scripted(json({ n: 1 }), json({ n: 2 }));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    await fetcher.json('GET', '/domains');
    await fetcher.json('GET', '/domains');
    expect(calls[1]?.headers['If-None-Match']).toBeUndefined();
  });

  it('returns undefined for 204', async () => {
    const { fetchStub } = scripted(new Response(null, { status: 204 }));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    expect(await fetcher.json('DELETE', '/domains/a/branches/b')).toBeUndefined();
  });

  it.each([
    [401, 'unauthorized', UnauthorizedError],
    [403, 'forbidden', ForbiddenError],
    [404, 'not-found', NotFoundError],
    [409, 'conflict', ConflictError],
    [409, 'protected-branch', ProtectedBranchError],
    [409, 'approvals-required', ApprovalsRequiredError],
    [409, 'merge-conflicts', MergeConflictsError],
    [422, 'validation-failed', ValidationError],
  ])('maps %i %s to its typed error', async (status, code, type) => {
    const { fetchStub } = scripted(problem(status, code, { detail: 'why' }));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    const error: unknown = await fetcher.json('GET', '/x').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(type);
    expect((error as Error).message).toBe('why');
  });

  it('reports a request that got no answer as a NetworkError', async () => {
    const { fetchStub } = scripted(new TypeError('Failed to fetch'));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    const error: unknown = await fetcher.json('GET', '/x').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(NetworkError);
    expect(describeError(error)).toContain('unreachable');
  });

  it('survives an error body that is not a problem', async () => {
    const { fetchStub } = scripted(new Response('<html>bad gateway</html>', { status: 502 }));
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    const error: unknown = await fetcher.json('GET', '/x').catch((e: unknown) => e);
    expect((error as { status: number }).status).toBe(502);
  });
});

describe('describeError', () => {
  it('names the first finding of a refused request and hints at the token on 401', async () => {
    const { fetchStub } = scripted(
      problem(422, 'validation-failed', {
        detail: '1 validation issue(s); nothing was written.',
        issues: [{ code: 'x', message: 'Attribute tier is not defined.' }],
      }),
      problem(401, 'unauthorized', { detail: 'No credential.' }),
    );
    const fetcher = createFetcher({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    const refused: unknown = await fetcher.json('POST', '/x').catch((e: unknown) => e);
    expect(describeError(refused)).toBe(
      '1 validation issue(s); nothing was written. Attribute tier is not defined.',
    );
    const anonymous: unknown = await fetcher.json('GET', '/x').catch((e: unknown) => e);
    expect(describeError(anonymous)).toContain('API token in Settings');
    expect(describeError(new Error('boom'))).toBe('boom');
    expect(describeError('x')).toBe('The request failed.');
  });
});

describe('session support', () => {
  const base = { baseUrl: 'https://keeper.test' };

  it('passes credentials to fetch and the method to the headers hook', async () => {
    const seen: string[] = [];
    let credentials: RequestCredentials | undefined;
    const fetchStub = ((_input: RequestInfo | URL, init?: RequestInit) => {
      credentials = init?.credentials;
      return Promise.resolve(json({}));
    }) as typeof fetch;
    const fetcher = createFetcher({
      ...base,
      fetch: fetchStub,
      credentials: 'include',
      headers: (method) => {
        seen.push(method);
        const extra: Record<string, string> = method === 'GET' ? {} : { 'X-CSRF': 't' };
        return extra;
      },
    });
    await fetcher.json('GET', '/a');
    await fetcher.json('POST', '/a', { body: {} });
    expect(credentials).toBe('include');
    expect(seen).toEqual(['GET', 'POST']);
  });

  it('calls onUnauthorized on a 401 and still throws', async () => {
    const { fetchStub } = scripted(problem(401, 'unauthorized'));
    const onUnauthorized = vi.fn();
    const fetcher = createFetcher({ ...base, fetch: fetchStub, onUnauthorized });
    await expect(fetcher.json('GET', '/a')).rejects.toBeInstanceOf(UnauthorizedError);
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });

  it('retries once after a CSRF rejection when the hook renews the token', async () => {
    let token = 'old';
    const { calls, fetchStub } = scripted(
      problem(403, 'forbidden', { reason: 'csrf' }),
      json({ ok: true }),
    );
    const fetcher = createFetcher({
      ...base,
      fetch: fetchStub,
      headers: () => ({ 'X-CSRF': token }),
      onCsrfRejected: () => {
        token = 'new';
        return Promise.resolve(true);
      },
    });
    await expect(fetcher.json('POST', '/a', { body: {} })).resolves.toEqual({ ok: true });
    expect(calls.map((call) => call.headers['X-CSRF'])).toEqual(['old', 'new']);
  });

  it('does not retry twice, nor for other 403s, nor when the hook declines', async () => {
    const csrf = () => problem(403, 'forbidden', { reason: 'csrf' });
    const twice = scripted(csrf(), csrf());
    const hook = vi.fn(() => Promise.resolve(true));
    await expect(
      createFetcher({ ...base, fetch: twice.fetchStub, onCsrfRejected: hook }).json('POST', '/a'),
    ).rejects.toBeInstanceOf(ForbiddenError);
    expect(twice.calls).toHaveLength(2);

    const other = scripted(problem(403, 'forbidden'));
    const otherHook = vi.fn(() => Promise.resolve(true));
    await expect(
      createFetcher({ ...base, fetch: other.fetchStub, onCsrfRejected: otherHook }).json(
        'POST',
        '/a',
      ),
    ).rejects.toBeInstanceOf(ForbiddenError);
    expect(otherHook).not.toHaveBeenCalled();

    const declined = scripted(csrf());
    await expect(
      createFetcher({
        ...base,
        fetch: declined.fetchStub,
        onCsrfRejected: () => Promise.resolve(false),
      }).json('POST', '/a'),
    ).rejects.toBeInstanceOf(ForbiddenError);
    expect(declined.calls).toHaveLength(1);
  });
});
