import { ApiError } from './errors';
import { createHttpApi } from './http';

function stubFetch(response: Response) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fetchStub = ((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: input instanceof Request ? input.url : input.toString(), init });
    return Promise.resolve(response);
  }) as typeof fetch;
  return { calls, fetchStub };
}

const bodyOf = (init: RequestInit | undefined) => (typeof init?.body === 'string' ? init.body : '');

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

describe('http adapter', () => {
  it('builds Domain and Branch paths with encoded names and scope parameters', async () => {
    const { calls, fetchStub } = stubFetch(json({ revision: 'r1', rows: [], next_cursor: null }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test/', fetch: fetchStub });

    await api.records.list({ domain: 'supply', branch: 'model/risk', at: 'abc' }, 'Supplier', {
      where: { attr: 'country', op: 'eq', value: 'IT' },
      limit: 10,
    });

    expect(calls).toHaveLength(1);
    const url = new URL(calls[0]?.url ?? '');
    expect(url.origin + url.pathname).toBe(
      'https://keeper.example.test/api/v1/domains/supply/kinds/Supplier/records',
    );
    expect(url.searchParams.get('branch')).toBe('model/risk');
    expect(url.searchParams.get('at')).toBe('abc');
    expect(url.searchParams.get('limit')).toBe('10');
    expect(JSON.parse(url.searchParams.get('where') ?? '')).toEqual({
      attr: 'country',
      op: 'eq',
      value: 'IT',
    });
  });

  it('URL-encodes hierarchical Branch names in paths', async () => {
    const { calls, fetchStub } = stubFetch(json({ items: [], next_cursor: null }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.revisions.log('supply', 'model/risk');
    expect(calls[0]?.url).toBe(
      'https://keeper.example.test/api/v1/domains/supply/branches/model%2Frisk/log',
    );
  });

  it('posts JSON bodies', async () => {
    const { calls, fetchStub } = stubFetch(json({ revision: 'r1', results: [] }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.search.run({ domain: 'supply' }, { q: 'brevik', kinds: ['Supplier'] });
    expect(calls[0]?.init?.method).toBe('POST');
    expect(JSON.parse(bodyOf(calls[0]?.init))).toEqual({ q: 'brevik', kinds: ['Supplier'] });
  });

  it('turns problem details into ApiError', async () => {
    const { fetchStub } = stubFetch(
      json(
        { type: 'about:blank', title: 'Not found', status: 404, detail: 'No such Domain.' },
        404,
      ),
    );
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    const error = await api.domains.get('x').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe('No such Domain.');
    expect((error as ApiError).status).toBe(404);
  });

  it('treats 204 as no content', async () => {
    const { fetchStub } = stubFetch(new Response(null, { status: 204 }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await expect(api.branches.delete('supply', 'tmp')).resolves.toBeUndefined();
  });
});
