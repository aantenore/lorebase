import { urlOf } from '@/test/fetch';
import { createSessionClient, safeReturnTo, type SessionInfo } from './session';

const SESSION: SessionInfo = {
  principal_id: 'p1',
  display_name: 'Ada Lovelace',
  csrf_header: 'X-CSRF-Token',
  csrf_token: 'tok',
  expires_at: '2030-01-01T00:00:00Z',
  idle_timeout_s: 1800,
};

interface Call {
  url: string;
  init: RequestInit | undefined;
}

function stub(...responses: Response[]) {
  const calls: Call[] = [];
  const queue = [...responses];
  const fetchStub = ((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: urlOf(input), init });
    return Promise.resolve(queue.shift() ?? new Response('{}', { status: 200 }));
  }) as typeof fetch;
  return { calls, fetchStub };
}

const ok = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });

describe('session client', () => {
  it('loads the session with credentials and keeps it', async () => {
    const { calls, fetchStub } = stub(ok(SESSION));
    const client = createSessionClient({ baseUrl: 'https://keeper.test', fetch: fetchStub });
    expect(client.info()).toBeNull();
    const notified = vi.fn();
    client.subscribe(notified);
    await expect(client.load()).resolves.toEqual(SESSION);
    expect(calls[0]?.url).toBe('https://keeper.test/api/v1/auth/session');
    expect(calls[0]?.init?.credentials).toBe('include');
    expect(client.info()).toEqual(SESSION);
    expect(notified).toHaveBeenCalledOnce();
  });

  it('answers null on 401 and forgets an earlier session', async () => {
    const { fetchStub } = stub(ok(SESSION), new Response('{}', { status: 401 }));
    const client = createSessionClient({ baseUrl: '', fetch: fetchStub });
    await client.load();
    await expect(client.load()).resolves.toBeNull();
    expect(client.info()).toBeNull();
  });

  it('fails on other statuses', async () => {
    const { fetchStub } = stub(new Response('{}', { status: 404 }));
    const client = createSessionClient({ baseUrl: '', fetch: fetchStub });
    await expect(client.load()).rejects.toThrow(/404/);
  });

  it('adds the CSRF header to unsafe methods only', async () => {
    const { fetchStub } = stub(ok(SESSION));
    const client = createSessionClient({ baseUrl: '', fetch: fetchStub });
    expect(client.csrfHeaders('POST')).toEqual({});
    await client.load();
    expect(client.csrfHeaders('GET')).toEqual({});
    expect(client.csrfHeaders('head')).toEqual({});
    for (const method of ['POST', 'PATCH', 'PUT', 'DELETE']) {
      expect(client.csrfHeaders(method)).toEqual({ 'X-CSRF-Token': 'tok' });
    }
  });

  it('signs out with the CSRF header, clears the session and returns the provider URL', async () => {
    const { calls, fetchStub } = stub(ok(SESSION), ok({ logout_url: 'https://idp.test/bye' }));
    const client = createSessionClient({ baseUrl: '', fetch: fetchStub });
    await client.load();
    await expect(client.logout()).resolves.toBe('https://idp.test/bye');
    expect(calls[1]?.url).toBe('/api/v1/auth/logout');
    expect(calls[1]?.init?.method).toBe('POST');
    expect(calls[1]?.init?.credentials).toBe('include');
    expect(calls[1]?.init?.headers).toMatchObject({ 'X-CSRF-Token': 'tok' });
    expect(client.info()).toBeNull();
  });

  it('returns null when Keeper has no end-session URL', async () => {
    const { fetchStub } = stub(ok({ logout_url: null }));
    const client = createSessionClient({ baseUrl: '', fetch: fetchStub });
    await expect(client.logout()).resolves.toBeNull();
  });

  it('builds the login URL with a sanitized return path', () => {
    const client = createSessionClient({ baseUrl: 'https://keeper.test/' });
    expect(client.loginUrl('/supply/explorer?branch=a')).toBe(
      'https://keeper.test/api/v1/auth/login?return_to=%2Fsupply%2Fexplorer%3Fbranch%3Da',
    );
    for (const bad of ['//evil.test', '/\\evil.test', 'https://evil.test', 'x', '']) {
      expect(safeReturnTo(bad)).toBe('/');
      expect(client.loginUrl(bad)).toMatch(/return_to=%2F$/);
    }
  });
});
