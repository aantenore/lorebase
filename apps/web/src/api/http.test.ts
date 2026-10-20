import { ApiError } from './errors';
import { createHttpApi } from './http';

function stubFetch(response: Response) {
  const calls: { url: string; init: RequestInit | undefined }[] = [];
  const fetchStub = ((input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: input instanceof Request ? input.url : input.toString(), init });
    return Promise.resolve(response.clone());
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

  it('stores a Resolution at the Conflict path and merges with a POST', async () => {
    const { calls, fetchStub } = stubFetch(json({ fingerprint: 'ab12', choice: 'ours' }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.conflicts.resolve('supply', '41', {
      fingerprint: 'ab12',
      choice: 'custom',
      values: { 'S-1004': 'low' },
    });
    expect(calls[0]?.url).toBe(
      'https://keeper.example.test/api/v1/domains/supply/proposals/41/conflicts/ab12/resolution',
    );
    expect(JSON.parse(bodyOf(calls[0]?.init))).toEqual({
      choice: 'custom',
      values: { 'S-1004': 'low' },
    });

    await api.conflicts.resolveMany('supply', '41', {
      resolutions: [{ fingerprint: 'ab12', choice: 'theirs' }],
    });
    expect(calls[1]?.url).toMatch(/proposals\/41\/conflicts:resolve$/);
    await api.proposals.merge('supply', '41');
    expect(calls[2]?.url).toMatch(/proposals\/41\/merge$/);
    expect(calls[2]?.init?.method).toBe('POST');
  });

  it('maps the merge-conflicts problem to the conflicts outcome', async () => {
    const attempt = {
      proposal: 'prp_1',
      base: 'b',
      ours: 'o',
      theirs: 't',
      conflicts: [],
      resolutions: [],
      auto_resolutions: [],
      open_conflicts: 1,
    };
    const { calls, fetchStub } = stubFetch(
      json(
        {
          type: 'urn:lorebase:problem:merge-conflicts',
          title: 'Merge has Conflicts',
          status: 409,
          code: 'merge-conflicts',
          attempt,
        },
        409,
      ),
    );
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await expect(api.proposals.merge('supply', 'prp_1')).resolves.toEqual({
      status: 'conflicts',
      attempt,
    });
    expect(calls[0]?.url).toMatch(/proposals\/prp_1\/merge$/);

    const listing = stubFetch(json(attempt));
    const lister = createHttpApi({
      baseUrl: 'https://keeper.example.test',
      fetch: listing.fetchStub,
    });
    await lister.conflicts.list('supply', 'prp_1', { kind: ['DV1', 'XS8'], status: 'open' });
    const url = new URL(listing.calls[0]?.url ?? '');
    expect(url.searchParams.get('kind')).toBe('DV1,XS8');
    expect(url.searchParams.get('status')).toBe('open');
  });

  it('posts schema change sets to the Branch', async () => {
    const { calls, fetchStub } = stubFetch(
      json({ valid: true, revision: null, violations: [], migrations: [] }),
    );
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.schema.applyChanges('supply', 'model/risk', {
      changes: [{ command: 'remove_kind', target: 'knd_x', arguments: {} }],
      dry_run: true,
    });
    expect(calls[0]?.url).toBe(
      'https://keeper.example.test/api/v1/domains/supply/branches/model%2Frisk/schema/changes',
    );
    expect(JSON.parse(bodyOf(calls[0]?.init))).toMatchObject({ dry_run: true });
  });
});
