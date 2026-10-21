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

  it('uploads the CSV files of schema:infer as multipart with the options as JSON text', async () => {
    const { calls, fetchStub } = stubFetch(json({ domain: 'supply', yaml: '' }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.inference.infer('supply', {
      files: [new File(['a\n1\n'], 'supplier.csv'), new File(['b\n2\n'], 'part.csv')],
      options: { enum_max_cardinality: 20 },
    });
    expect(calls[0]?.url).toBe('https://keeper.example.test/api/v1/domains/supply/schema:infer');
    expect(calls[0]?.init?.method).toBe('POST');
    const form = calls[0]?.init?.body;
    expect(form).toBeInstanceOf(FormData);
    if (!(form instanceof FormData)) return;
    expect(form.getAll('files').map((f) => (f instanceof File ? f.name : f))).toEqual([
      'supplier.csv',
      'part.csv',
    ]);
    expect(form.get('options')).toBe('{"enum_max_cardinality":20}');
    // The browser sets the multipart boundary; the adapter must not set a Content-Type.
    expect(calls[0]?.init?.headers).not.toHaveProperty('Content-Type');
  });

  it('posts a Postgres source reference to schema:infer as JSON', async () => {
    const { calls, fetchStub } = stubFetch(json({ domain: 'supply', yaml: '' }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.inference.infer('supply', {
      postgres: { dsn: 'secret://env/ERP_DSN', schema: 'public', tables: ['vendors'] },
    });
    expect(JSON.parse(bodyOf(calls[0]?.init))).toEqual({
      postgres: { dsn: 'secret://env/ERP_DSN', schema: 'public', tables: ['vendors'] },
    });
  });

  it('calls the admin routes of a Domain', async () => {
    const { calls, fetchStub } = stubFetch(json({ items: [], next_cursor: null }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await api.admin.apiKeys.list('supply', { limit: 5 });
    await api.admin.apiKeys.create('supply', { name: 'agent', role: 'agent-reader', ttl_days: 30 });
    await api.admin.grants.list('supply');
    await api.admin.audit.list('supply', { limit: 20, cursor: 'c1' });
    await api.admin.principals.list({ type: 'service' });
    expect(
      calls.map(
        (c) =>
          `${c.init?.method ?? 'GET'} ${c.url.replace('https://keeper.example.test/api/v1', '')}`,
      ),
    ).toEqual([
      'GET /domains/supply/api-keys?limit=5',
      'POST /domains/supply/api-keys',
      'GET /domains/supply/grants',
      'GET /domains/supply/audit?limit=20&cursor=c1',
      'GET /principals?type=service',
    ]);
    expect(JSON.parse(bodyOf(calls[1]?.init))).toEqual({
      name: 'agent',
      role: 'agent-reader',
      ttl_days: 30,
    });
  });

  it('revokes an API key with DELETE and accepts the empty answer', async () => {
    const { calls, fetchStub } = stubFetch(new Response(null, { status: 204 }));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    await expect(api.admin.apiKeys.revoke('supply', 'key 1')).resolves.toBeUndefined();
    expect(calls[0]?.init?.method).toBe('DELETE');
    expect(calls[0]?.url).toBe(
      'https://keeper.example.test/api/v1/domains/supply/api-keys/key%201',
    );
  });

  it('reads the runs of one Source and maps them to the screen model', async () => {
    const { calls, fetchStub } = stubFetch(
      json({
        items: [
          {
            id: 'run_9',
            source: 'src_vendors',
            status: 'failed',
            started_at: '2026-10-19T02:00:00Z',
            finished_at: '2026-10-19T02:00:05Z',
            stats: {
              branch: 'source/erp.vendors',
              rows: { read: 31, mapped: 30, failed: 1 },
              counts: { added: 1, changed: 5, removed: 0 },
            },
            revision: null,
            proposal_id: '40',
            error: { type: 'TimeoutError', message: 'timed out' },
          },
        ],
        next_cursor: 'c2',
      }),
    );
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    const page = await api.sources.runs('supply', { source: 'erp.vendors', limit: 5 });

    const url = new URL(calls[0]?.url ?? '');
    expect(url.pathname).toBe('/api/v1/domains/supply/sources/erp.vendors/runs');
    expect(url.searchParams.get('limit')).toBe('5');
    expect(page.next_cursor).toBe('c2');
    expect(page.items[0]).toEqual({
      id: 'run_9',
      source: 'erp.vendors',
      status: 'failed',
      trigger: null,
      started_at: '2026-10-19T02:00:00Z',
      finished_at: '2026-10-19T02:00:05Z',
      rows_read: 31,
      changes: { added: 1, updated: 5, deleted: 0 },
      branch: 'source/erp.vendors',
      proposal: '40',
      error: 'timed out',
    });
  });

  it('merges the runs of every Source of the schema, newest first', async () => {
    const run = (id: string, started: string) => ({
      id,
      source: 'src',
      status: 'succeeded',
      started_at: started,
      finished_at: started,
      stats: {},
      revision: null,
      proposal_id: null,
      error: null,
    });
    const answers: Record<string, unknown> = {
      '/api/v1/domains/supply/schema': { sources: [{ api_name: 'a' }, { api_name: 'b' }] },
      '/api/v1/domains/supply/sources/a/runs': {
        items: [run('a1', '2026-10-18T00:00:00Z')],
        next_cursor: null,
      },
      '/api/v1/domains/supply/sources/b/runs': {
        items: [run('b1', '2026-10-19T00:00:00Z'), run('b0', '2026-10-17T00:00:00Z')],
        next_cursor: 'more',
      },
    };
    const fetchStub = ((input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : input.toString());
      return Promise.resolve(json(answers[url.pathname]));
    }) as typeof fetch;
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });

    const page = await api.sources.runs('supply', { limit: 2 });
    expect(page.items.map((r) => [r.id, r.source])).toEqual([
      ['b1', 'b'],
      ['a1', 'a'],
    ]);
    expect(page.next_cursor).toBeNull();
  });

  it('queues a sync of a Source', async () => {
    const queued = { job_id: 7, source: 'src_vendors', api_name: 'erp.vendors', dry_run: false };
    const { calls, fetchStub } = stubFetch(json(queued, 202));
    const api = createHttpApi({ baseUrl: 'https://keeper.example.test', fetch: fetchStub });
    expect(await api.sources.sync('supply', 'erp.vendors')).toEqual(queued);
    expect(calls[0]?.url).toBe(
      'https://keeper.example.test/api/v1/domains/supply/sources/erp.vendors/sync',
    );
    expect(calls[0]?.init?.method).toBe('POST');
  });
});
