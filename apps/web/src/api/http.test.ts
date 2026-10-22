// @vitest-environment node
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { urlOf } from '@/test/fetch';
import { MergeConflictsError, NotFoundError, ValidationError } from './errors';
import { createHttpApi } from './http';

interface Call {
  url: URL;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

type Handler = (call: Call) => Response;

/** A Keeper that answers by `METHOD /path` (the path after /api/v1). */
function fakeKeeper(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  const fetchStub = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(urlOf(input));
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : init?.body;
    const call: Call = {
      url,
      method: init?.method ?? 'GET',
      headers: (init?.headers ?? {}) as Record<string, string>,
      body,
    };
    calls.push(call);
    const key = `${call.method} ${decodeURIComponent(url.pathname.replace('/api/v1', ''))}`;
    const route = routes[key];
    if (route === undefined) return Promise.resolve(problem(404, 'not-found', `No route ${key}`));
    if (route instanceof Response) return Promise.resolve(route.clone());
    return Promise.resolve(typeof route === 'function' ? (route as Handler)(call) : json(route));
  }) as typeof fetch;
  const api = createHttpApi({ baseUrl: 'https://keeper.test', fetch: fetchStub });
  return { api, calls };
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const problem = (status: number, code: string, detail = 'x', extra: object = {}) =>
  json(
    { type: `urn:lorebase:problem:${code}`, title: 'T', status, code, detail, ...extra },
    status,
  );

const fixture = (name: string): unknown =>
  JSON.parse(
    readFileSync(fileURLToPath(new URL(`./live/fixtures/${name}`, import.meta.url)), 'utf8'),
  );

const branchOut = (name: string) => ({
  name,
  head: 'a'.repeat(64),
  protected: name === 'main',
  default: name === 'main',
  materialization: 'full',
  version: 1,
  created_at: '2026-10-21T00:00:00Z',
  created_by: 'anonymous',
});

const record = (key: string) => ({
  kind: 'Supplier',
  key,
  title: `Supplier ${key}`,
  values: { risk_score: '32', status: 'active' },
  updated_revision: 'r1',
  updated_at: null,
  links: null,
  lineage: null,
  included: null,
});

describe('live adapter: reads', () => {
  it('lists Records with the scope, the filter and Keeper ordering syntax', async () => {
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/kinds/Supplier/records': {
        revision: 'r1',
        served_from: 'materialized',
        items: [record('S-1001')],
        next_cursor: 'c2',
      },
    });
    const page = await api.records.list({ domain: 'supply', branch: 'model/risk' }, 'Supplier', {
      where: { attr: 'country', op: 'eq', value: 'IT' },
      order_by: [
        { attr: 'risk_score', dir: 'desc' },
        { attr: 'legal_name', dir: 'asc' },
      ],
      limit: 10,
    });
    const url = calls[0]?.url;
    expect(url?.searchParams.get('branch')).toBe('model/risk');
    expect(url?.searchParams.get('order_by')).toBe('risk_score:desc,legal_name:asc');
    expect(JSON.parse(url?.searchParams.get('where') ?? '')).toMatchObject({ attr: 'country' });
    expect(page).toEqual({
      revision: 'r1',
      next_cursor: 'c2',
      rows: [
        {
          ref: 'Supplier/S-1001',
          kind: 'Supplier',
          key: 'S-1001',
          title: 'Supplier S-1001',
          attributes: { risk_score: '32', status: 'active' },
          derived: {},
        },
      ],
    });
  });

  it('URL-encodes hierarchical Branch names in paths', async () => {
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/branches/model/risk/log': { branch: 'model/risk', head: 'h', items: [] },
    });
    await api.revisions.log('supply', 'model/risk');
    expect(calls[0]?.url.pathname).toBe('/api/v1/domains/supply/branches/model%2Frisk/log');
  });

  it('maps Revisions and the distance of a Branch to the default Branch', async () => {
    const { api } = fakeKeeper({
      'GET /domains/supply/branches': {
        items: [branchOut('main'), branchOut('work')],
        next_cursor: null,
      },
      'GET /domains/supply/tags': {
        items: [{ name: 'v1', revision: 'b'.repeat(64), created_at: 'x', created_by: 'y' }],
        next_cursor: null,
      },
      'GET /domains/supply/branches/main': { ...branchOut('main'), ahead: 0, behind: 0 },
      'GET /domains/supply/branches/work': { ...branchOut('work'), ahead: 6, behind: 2 },
      'GET /domains/supply/revisions/r1': {
        id: 'r1',
        parents: [],
        root: 'root',
        generation: 1,
        author: 'anonymous',
        on_behalf_of: null,
        message: 'Import',
        origin: { type: 'import', ref: 'inv_1' },
        stats: { changes: 48, partitions_changed: 1 },
        created_at: '2026-10-21T00:00:00Z',
      },
    });
    const branches = await api.branches.list('supply');
    expect(branches.items.map((b) => [b.name, b.kind, b.ahead_default, b.behind_default])).toEqual([
      ['main', 'branch', 0, 0],
      ['work', 'branch', 6, 2],
      ['v1', 'tag', 0, 0],
    ]);
    const revision = await api.revisions.get('supply', 'r1');
    expect(revision).toMatchObject({
      author: { principal: 'anonymous', on_behalf_of: null },
      origin: { type: 'import', ref: 'inv_1' },
      stats: { partitions_changed: 1, changes: 48 },
      domain: 'supply',
    });
  });

  it('finds a tag when a Branch of that name does not exist', async () => {
    const { api } = fakeKeeper({
      'GET /domains/supply/tags': {
        items: [{ name: 'v1', revision: 'rev', created_at: 'x', created_by: 'y' }],
        next_cursor: null,
      },
    });
    expect(await api.branches.get('supply', 'v1')).toMatchObject({ kind: 'tag', head: 'rev' });
    await expect(api.branches.get('supply', 'nope')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('maps Links to the far Record and search hits to plain snippets', async () => {
    const { api } = fakeKeeper({
      'GET /domains/supply/kinds/Part/records/P-1/links/supplied_by': {
        revision: 'r1',
        items: [
          {
            relation: 'supplied_by',
            direction: 'forward',
            from_key: 'P-1',
            to_key: 'S-9',
            values: { is_primary: true },
            target: { kind: 'Supplier', key: 'S-9', title: 'Nimvane' },
          },
        ],
        next_cursor: null,
      },
      'POST /domains/supply/search': {
        revision: 'r1',
        mode: 'lexical',
        items: [
          {
            kind: 'Supplier',
            key: 'S-1',
            title: 'Kavsen',
            snippet: '<b>Kav</b>sen Alloys',
            score: 2,
          },
        ],
      },
    });
    const links = await api.records.links({ domain: 'supply' }, 'Part', 'P-1', 'supplied_by');
    expect(links.links).toEqual([
      { ref: 'Supplier/S-9', title: 'Nimvane', attributes: { is_primary: true } },
    ]);
    const found = await api.search.run({ domain: 'supply', branch: 'work' }, { q: 'kav' });
    expect(found.results[0]).toMatchObject({ ref: 'Supplier/S-1', snippet: 'Kavsen Alloys' });
  });

  it('sends seeds as addresses and maps the graph to references', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/neighborhood': {
        revision: 'r1',
        nodes: [record('S-1'), { ...record('P-1'), kind: 'Part' }],
        edges: [
          {
            relation: 'supplies',
            from: { kind: 'Supplier', key: 'S-1' },
            to: { kind: 'Part', key: 'P-1' },
            values: {},
          },
        ],
        truncated: true,
      },
    });
    const graph = await api.query.neighborhood(
      { domain: 'supply', branch: 'work' },
      { seeds: ['Supplier/S-1'], depth: 2, max_nodes: 50 },
    );
    expect(calls[0]?.body).toEqual({
      seeds: [{ kind: 'Supplier', key: 'S-1' }],
      depth: 2,
      max_nodes: 50,
      branch: 'work',
    });
    expect(graph.edges).toEqual([{ from: 'Supplier/S-1', to: 'Part/P-1', relation: 'supplies' }]);
    expect(graph.truncated).toBe(true);
    expect(graph.nodes[1]).toMatchObject({ ref: 'Part/P-1', kind: 'Part' });
  });

  it('reads lineage and history of a Record from their own routes', async () => {
    const base = {
      revision: 'r9',
      served_from: 'tree',
      branch: 'work',
      kind: 'Supplier',
      key: 'S-1',
    };
    const lineage = {
      ...base,
      created: null,
      changes: 2,
      attributes: [],
      sources: [],
      proposals: [],
    };
    const history = { ...base, items: [], next_cursor: 'c2' };
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/kinds/Supplier/records/S-1/lineage': lineage,
      'GET /domains/supply/kinds/Supplier/records/S-1/history': history,
    });
    const scope = { domain: 'supply', branch: 'work' };
    expect(await api.records.lineage(scope, 'Supplier', 'S-1')).toEqual(lineage);
    expect(calls[0]?.url.searchParams.get('branch')).toBe('work');
    const page = await api.records.history(scope, 'Supplier', 'S-1', {
      values: true,
      limit: 10,
      cursor: 'c1',
    });
    expect(page.next_cursor).toBe('c2');
    const query = calls[1]?.url.searchParams;
    expect([
      query?.get('branch'),
      query?.get('values'),
      query?.get('limit'),
      query?.get('cursor'),
    ]).toEqual(['work', 'true', '10', 'c1']);
    await expect(
      api.records.lineage({ ...scope, at: 'r1' }, 'Supplier', 'S-1'),
    ).rejects.toMatchObject({
      status: 501,
    });
  });

  it('maps a diff to a summary per Kind and changes per Record', async () => {
    const { api } = fakeKeeper({
      'GET /domains/supply/diff': {
        from_revision: 'a',
        to_revision: 'b',
        total: 2,
        summary: [
          {
            partition: 'k:1',
            element: 'kind',
            name: 'Supplier',
            added: 0,
            removed: 0,
            modified: 1,
          },
        ],
        items: [
          {
            element: 'kind',
            change: 'modified',
            partition: 'k:1',
            kind: 'Supplier',
            key: 'S-1',
            changes: [{ name: 'risk_score', old: '32', new: '80' }],
          },
        ],
        next_cursor: null,
      },
    });
    const diff = await api.revisions.diff('supply', { from: 'main', to: 'work' });
    expect(diff.summary).toEqual({ Supplier: { added: 0, modified: 1, removed: 0 } });
    expect(diff.changes[0]).toEqual({
      element: 'record',
      change: 'modified',
      kind: 'Supplier',
      ref: 'Supplier/S-1',
      attributes: [{ attribute: 'risk_score', before: '32', after: '80' }],
    });
  });

  it('serves the schema as the lists the screens use', async () => {
    const { api } = fakeKeeper({ 'GET /domains/supply/schema': fixture('supply-schema.json') });
    const schema = await api.schema.get({ domain: 'supply', branch: 'main' });
    expect(schema.kinds).toHaveLength(6);
    expect(schema.revision).toHaveLength(64);
  });
});

describe('live adapter: writes', () => {
  it('creates a Branch from a source and answers with its distance', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/branches': json(branchOut('work'), 201),
      'GET /domains/supply/branches/work': { ...branchOut('work'), ahead: 0, behind: 0 },
    });
    const created = await api.branches.create('supply', { name: 'work', source: 'main' });
    expect(calls[0]?.body).toEqual({ name: 'work', source: 'main' });
    expect(calls[0]?.headers['Idempotency-Key']).toMatch(/[0-9a-f-]{20,}/);
    expect(created).toMatchObject({ name: 'work', kind: 'branch', ahead_default: 0 });
  });

  it('reports a create Keeper answered with the existing resource as already existing', async () => {
    const { api } = fakeKeeper({
      // 200 instead of 201: Keeper replays the existing Branch for a create with an Idempotency-Key.
      'POST /domains/supply/branches': branchOut('main'),
    });
    await expect(api.branches.create('supply', { name: 'main' })).rejects.toMatchObject({
      status: 409,
      code: 'already-exists',
    });
  });

  it('sends a schema change set as a document, and a refused one is an answer', async () => {
    const issue = {
      code: 'schema.unknown_reference',
      severity: 'error',
      path: ['kinds', 'A', 'attributes', 'b'],
      message: 'No such Kind.',
    };
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/branches/work/schema/changes': (call: Call) =>
        (call.body as { dry_run?: boolean }).dry_run === true
          ? problem(422, 'validation-failed', '1 validation issue(s); nothing was written.', {
              issues: [issue],
              issue_count: 1,
            })
          : json({
              status: 'committed',
              branch: 'work',
              head: 'h',
              revision: 'r2',
              changes: [{ op: 'create', section: 'kinds', id: 'knd_1', api_name: 'Contract' }],
              change_count: 1,
              data_changes: 3,
              issues: [],
              issue_count: 0,
            }),
    });
    const document = { format: 'lorebase.schema/v1', domain: { id: 'supply' }, kinds: { A: {} } };
    const refused = await api.schema.applyChanges('supply', 'work', {
      changes: [],
      document,
      dry_run: true,
    });
    expect(refused.valid).toBe(false);
    expect(refused.violations).toEqual([
      {
        code: 'schema.unknown_reference',
        message: 'No such Kind.',
        path: 'kinds/A/attributes/b',
        severity: 'error',
      },
    ]);

    const saved = await api.schema.applyChanges('supply', 'work', {
      changes: [],
      document,
      message: 'Add A',
    });
    expect(saved).toMatchObject({ valid: true, revision: 'r2', data_changes: 3 });
    expect(saved.migrations).toHaveLength(1);
    const sent = calls[1]?.body as { yaml: string; message: string };
    expect(sent.message).toBe('Add A');
    expect(sent.yaml).toContain('format: lorebase.schema/v1');

    await expect(api.schema.applyChanges('supply', 'work', { changes: [] })).rejects.toBeInstanceOf(
      ValidationError,
    );
  });

  it('merges a Proposal, or reports the Conflicts that remain', async () => {
    const attempt = {
      id: 'mga_1',
      proposal: 'prp_1',
      status: 'conflicted',
      result: 'conflicts',
      base: 'a',
      ours: 'b',
      theirs: 'c',
      virtual_base: false,
      created_at: 'x',
      created_by: null,
      revision: null,
      policy: {},
      open_conflicts: 1,
      resolved_conflicts: 0,
      counts: {},
      recomputed: true,
      stale: false,
      resolutions: [],
      unmatched_resolutions: [],
      auto_resolutions: [],
      conflicts: [{ id: 'f'.repeat(64), fingerprint: 'f'.repeat(64), status: 'open', kind: 'DV1' }],
    };
    let merged = false;
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/proposals/prp_1/merge': () =>
        merged
          ? json({
              status: 'merged',
              result: 'merged',
              revision: 'r9',
              proposal: { id: 'prp_1', status: 'merged' },
              attempt: { ...attempt, status: 'merged', conflicts: [] },
              source_deleted: false,
              replayed: false,
            })
          : problem(409, 'merge-conflicts', '1 Conflict(s) remain.', { attempt }),
    });
    const stopped = await api.proposals.merge('supply', 'prp_1');
    expect(stopped.status).toBe('conflicts');
    if (stopped.status === 'conflicts') expect(stopped.attempt.conflicts).toHaveLength(1);
    merged = true;
    const done = await api.proposals.merge('supply', 'prp_1');
    expect(done).toMatchObject({ status: 'merged', revision: 'r9', result: 'merged' });
    expect(calls[0]?.headers['Idempotency-Key']).toBeDefined();
  });

  it('lets other merge failures through as typed errors', async () => {
    const { api } = fakeKeeper({
      'POST /domains/supply/proposals/prp_1/merge': problem(
        409,
        'approvals-required',
        'Needs 1 approval.',
      ),
    });
    await expect(api.proposals.merge('supply', 'prp_1')).rejects.toMatchObject({
      code: 'approvals-required',
    });
    expect(MergeConflictsError).toBeDefined();
  });

  it('stores Resolutions with the decision in the body and the fingerprint in the path', async () => {
    const fingerprint = 'a'.repeat(64);
    const { api, calls } = fakeKeeper({
      [`POST /domains/supply/proposals/prp_1/conflicts/${fingerprint}/resolution`]: {
        fingerprint,
        choice: 'custom',
        value: '90',
        resolver: 'anonymous',
        at: 'x',
        matched: true,
      },
    });
    const stored = await api.conflicts.resolve('supply', 'prp_1', {
      fingerprint,
      choice: 'custom',
      value: '90',
    });
    expect(calls[0]?.body).toEqual({ choice: 'custom', value: '90' });
    expect(stored.choice).toBe('custom');
  });

  it('answers a rejected Operation with the findings, and keeps review in the error path', async () => {
    const { api } = fakeKeeper({
      'POST /domains/supply/operations/update_supplier/execute': (call: Call) =>
        (call.body as { branch: string }).branch === 'main'
          ? problem(409, 'review-required', 'Needs review.')
          : problem(422, 'validation-failed', 'No.', {
              issues: [{ code: 'value.max', message: 'above max 100', path: ['changes', 0] }],
              edits: [{ op: 'upsert_record', kind: 'Supplier', key: 'S-1' }],
            }),
    });
    const rejected = await api.operations.execute('supply', 'update_supplier', {
      parameters: { key: 'S-1', risk_score: 999 },
      branch: 'work',
    });
    expect(rejected).toMatchObject({
      status: 'rejected',
      violations: [{ code: 'value.max', path: 'changes/0' }],
      edits: [{ action: 'upsert_record', ref: 'Supplier/S-1' }],
    });
    await expect(
      api.operations.execute('supply', 'update_supplier', { parameters: {}, branch: 'main' }),
    ).rejects.toMatchObject({ status: 409 });
  });

  it('takes the Proposal diff from the merge base', async () => {
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/proposals/prp_1': {
        id: 'prp_1',
        source_ref: 'work',
        target_ref: 'main',
        attempt: { base: 'basebase' },
      },
      'GET /domains/supply/diff': {
        from_revision: 'basebase',
        to_revision: 'x',
        total: 0,
        summary: [],
        items: [],
      },
    });
    await api.proposals.diff('supply', 'prp_1');
    expect(calls[1]?.url.searchParams.get('from')).toBe('basebase');
    expect(calls[1]?.url.searchParams.get('to')).toBe('work');
  });
});

describe('live adapter: agents, inference and webhooks', () => {
  it('identifies the caller and uploads files for schema inference', async () => {
    const { api } = fakeKeeper({
      'GET /auth/whoami': {
        id: 'anonymous',
        type: 'user',
        display_name: 'A',
        method: 'none',
        subjects: [],
        key_scopes: [],
        installation_admin: true,
      },
      'GET /domains/supply/webhooks': { items: [{ id: 'wh_1' }] },
      'DELETE /domains/supply/webhooks/wh_1': () => new Response(null, { status: 204 }),
    });
    expect((await api.auth.whoami()).installation_admin).toBe(true);
    expect(await api.webhooks.list('supply')).toEqual([{ id: 'wh_1' }]);
    await expect(api.webhooks.delete('supply', 'wh_1')).resolves.toBeUndefined();
  });

  it('asks for a context pack on the scope Branch', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/context': {
        domain: 'supply',
        branch: 'work',
        revision: 'r',
        etag: 'e',
        seeds: [],
        neighbors: [],
        links: {},
        budget: {},
      },
    });
    await api.agents.context({ domain: 'supply', branch: 'work' }, { refs: ['Supplier/S-1'] });
    expect(calls[0]?.body).toEqual({ branch: 'work', refs: ['Supplier/S-1'] });
  });

  it('uploads the CSV files of schema:infer as multipart with the options as JSON text', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/schema:infer': { domain: 'supply', yaml: '' },
    });
    await api.inference.infer('supply', {
      files: [new File(['a\n1\n'], 'supplier.csv'), new File(['b\n2\n'], 'part.csv')],
      options: { enum_max_cardinality: 20 },
    });
    expect(calls[0]?.url.pathname).toBe('/api/v1/domains/supply/schema:infer');
    expect(calls[0]?.method).toBe('POST');
    const form = calls[0]?.body;
    expect(form).toBeInstanceOf(FormData);
    if (!(form instanceof FormData)) return;
    expect(form.getAll('files').map((f) => (f instanceof File ? f.name : f))).toEqual([
      'supplier.csv',
      'part.csv',
    ]);
    expect(form.get('options')).toBe('{"enum_max_cardinality":20}');
    // The browser sets the multipart boundary; the adapter must not set a Content-Type.
    expect(calls[0]?.headers['Content-Type']).toBeUndefined();
  });

  it('posts a Postgres source reference to schema:infer as JSON', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/schema:infer': { domain: 'supply', yaml: '' },
    });
    await api.inference.infer('supply', {
      postgres: { dsn: 'secret://env/ERP_DSN', schema: 'public', tables: ['vendors'] },
    });
    expect(calls[0]?.body).toEqual({
      postgres: { dsn: 'secret://env/ERP_DSN', schema: 'public', tables: ['vendors'] },
    });
  });

  it('calls the admin routes of a Domain', async () => {
    const empty = { items: [], next_cursor: null };
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/api-keys': empty,
      'POST /domains/supply/api-keys': {},
      'GET /domains/supply/grants': empty,
      'GET /domains/supply/audit': empty,
      'GET /principals': empty,
    });
    await api.admin.apiKeys.list('supply', { limit: 5 });
    await api.admin.apiKeys.create('supply', { name: 'agent', role: 'agent-reader', ttl_days: 30 });
    await api.admin.grants.list('supply');
    await api.admin.audit.list('supply', { limit: 20, cursor: 'c1' });
    await api.admin.principals.list({ type: 'service' });
    expect(
      calls.map((c) => `${c.method} ${c.url.pathname.replace('/api/v1', '')}${c.url.search}`),
    ).toEqual([
      'GET /domains/supply/api-keys?limit=5',
      'POST /domains/supply/api-keys',
      'GET /domains/supply/grants',
      'GET /domains/supply/audit?limit=20&cursor=c1',
      'GET /principals?type=service',
    ]);
    expect(calls[1]?.body).toEqual({ name: 'agent', role: 'agent-reader', ttl_days: 30 });
  });

  it('revokes an API key with DELETE and accepts the empty answer', async () => {
    const { api, calls } = fakeKeeper({
      'DELETE /domains/supply/api-keys/key 1': () => new Response(null, { status: 204 }),
    });
    await expect(api.admin.apiKeys.revoke('supply', 'key 1')).resolves.toBeUndefined();
    expect(calls[0]?.method).toBe('DELETE');
    expect(calls[0]?.url.pathname).toBe('/api/v1/domains/supply/api-keys/key%201');
  });

  it('reads the runs of one Source and maps them to the screen model', async () => {
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/sources/erp.vendors/runs': {
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
      },
    });
    const page = await api.sources.runs('supply', { source: 'erp.vendors', limit: 5 });

    expect(calls[0]?.url.pathname).toBe('/api/v1/domains/supply/sources/erp.vendors/runs');
    expect(calls[0]?.url.searchParams.get('limit')).toBe('5');
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
    const { api } = fakeKeeper({
      'GET /domains/supply/schema': {
        domain: 'supply',
        branch: 'main',
        revision: 'r1',
        search: { modes: ['lexical'], embedding: null },
        schema: {
          sources: {
            src_a: { id: 'src_a', api_name: 'a' },
            src_b: { id: 'src_b', api_name: 'b' },
          },
        },
      },
      'GET /domains/supply/sources/a/runs': {
        items: [run('a1', '2026-10-18T00:00:00Z')],
        next_cursor: null,
      },
      'GET /domains/supply/sources/b/runs': {
        items: [run('b1', '2026-10-19T00:00:00Z'), run('b0', '2026-10-17T00:00:00Z')],
        next_cursor: 'more',
      },
    });

    const page = await api.sources.runs('supply', { limit: 2 });
    expect(page.items.map((r) => [r.id, r.source])).toEqual([
      ['b1', 'b'],
      ['a1', 'a'],
    ]);
    expect(page.next_cursor).toBeNull();
  });

  it('queues a sync of a Source', async () => {
    const queued = { job_id: 7, source: 'src_vendors', api_name: 'erp.vendors', dry_run: false };
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/sources/erp.vendors/sync': json(queued, 202),
    });
    expect(await api.sources.sync('supply', 'erp.vendors')).toEqual(queued);
    expect(calls[0]?.url.pathname).toBe('/api/v1/domains/supply/sources/erp.vendors/sync');
    expect(calls[0]?.method).toBe('POST');
  });
});

describe('live adapter: documents and extraction items', () => {
  const document = {
    id: 'doc_1',
    filename: 'c.md',
    media_type: 'text/markdown',
    sha256: 'ab',
    size: 12,
    text_sha256: null,
    metadata: { source: 'upload' },
    created_at: '2026-10-20T08:00:00Z',
    created_by: 'usr_1',
  };

  it('uploads a document as multipart with extract and tiers, and reports a duplicate', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/documents': json({ document, duplicate: true, job_id: null }, 200),
    });
    const file = new File(['# Contract'], 'c.md', { type: 'text/markdown' });
    const uploaded = await api.documents.upload('supply', { file, extract: true, tiers: [0, 1] });
    expect(uploaded).toEqual({ document, duplicate: true, job_id: null });
    const body = calls[0]?.body;
    expect(body).toBeInstanceOf(FormData);
    const form = body as FormData;
    expect((form.get('file') as File).name).toBe('c.md');
    expect(form.get('extract')).toBe('true');
    expect(form.get('tiers')).toBe('[0,1]');
    expect(calls[0]?.headers['Content-Type']).toBeUndefined();
  });

  it('sends a URL as JSON and leaves unset options out', async () => {
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/documents': json({ document, duplicate: false, job_id: 7 }, 201),
    });
    const uploaded = await api.documents.upload('supply', {
      url: 'https://docs.example.com/c.pdf',
      filename: 'c.pdf',
    });
    expect(uploaded.job_id).toBe(7);
    expect(calls[0]?.body).toEqual({ url: 'https://docs.example.com/c.pdf', filename: 'c.pdf' });
  });

  it('refuses an upload with the findings of the 422', async () => {
    const { api } = fakeKeeper({
      'POST /domains/supply/documents': problem(422, 'validation-failed', 'No.', {
        errors: [{ pointer: '/body/file', message: 'unsupported file type .exe', code: 'invalid' }],
      }),
    });
    const file = new File(['x'], 'c.exe');
    await expect(api.documents.upload('supply', { file })).rejects.toMatchObject({
      status: 422,
      fields: [{ pointer: '/body/file' }],
    });
  });

  it('lists documents and reads one with its jobs and its parsed text', async () => {
    const job = {
      id: 'job_1',
      status: 'succeeded',
      model: 'rules',
      prompt_version: 'extract-v1',
      branch: 'extract/job_1',
      proposal_id: 'prp_1',
      created_at: '2026-10-20T08:00:00Z',
      finished_at: '2026-10-20T08:01:00Z',
      usage: {},
      stats: {},
      error: null,
    };
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/documents': { items: [document], next_cursor: 'c2' },
      'GET /domains/supply/documents/doc_1': { ...document, extraction_jobs: [job] },
      'GET /domains/supply/documents/doc_1/text': {
        document_id: 'doc_1',
        text_sha256: 'ab'.repeat(32),
        text: 'Supplier S-1025',
        length: 40,
        truncated: true,
      },
    });
    const page = await api.documents.list('supply', { limit: 1 });
    expect(page).toEqual({ items: [document], next_cursor: 'c2' });
    expect(calls[0]?.url.searchParams.get('limit')).toBe('1');
    expect((await api.documents.get('supply', 'doc_1')).extraction_jobs[0]?.proposal_id).toBe(
      'prp_1',
    );
    expect(await api.documents.text('supply', 'doc_1')).toEqual({
      text: 'Supplier S-1025',
      length: 40,
      truncated: true,
    });
    // 404 until Scribe parsed the document.
    expect(await api.documents.text('supply', 'doc_2')).toBeNull();
    expect(calls).toHaveLength(4);
  });

  const itemOut = {
    id: 'pit_1',
    type: 'link',
    seq: 1,
    decision: 'pending',
    confidence: 0.6,
    tier: 0,
    model: null,
    kind: null,
    relation: 'with_supplier',
    key: null,
    attribute: null,
    value: null,
    current: null,
    from: 'C-3025',
    to: 'S-1025',
    values: {},
    subject: {},
    evidence: { document_id: 'doc_1', chunk: 0, start: 4, end: 20, quote: 'Kavvik' },
    match: {
      status: 'ambiguous',
      method: 'trigram',
      score: 0.9,
      candidates: [{ key: 'S-1025', title: 'Kavvik', score: 0.9, method: 'title' }],
    },
    edits: null,
    decided_by: null,
    decided_at: null,
    not_applied: null,
  };

  it('lists items with one query parameter per filter value', async () => {
    const { api, calls } = fakeKeeper({
      'GET /domains/supply/proposals/prp_1/items': { items: [itemOut], next_cursor: null },
    });
    const page = await api.proposals.items('supply', 'prp_1', {
      status: ['pending', 'accepted'],
      kind: ['with_supplier'],
      type: ['link'],
      min_confidence: 0.5,
      max_confidence: 1.01,
      limit: 10,
      cursor: 'x',
    });
    const search = calls[0]?.url.searchParams;
    expect(search?.getAll('status')).toEqual(['pending', 'accepted']);
    expect(search?.getAll('kind')).toEqual(['with_supplier']);
    expect(search?.getAll('type')).toEqual(['link']);
    expect(search?.get('min_confidence')).toBe('0.5');
    expect(search?.get('max_confidence')).toBe('1.01');
    expect(search?.get('limit')).toBe('10');
    expect(page.items[0]).toMatchObject({
      id: 'pit_1',
      from: 'C-3025',
      evidence: { start: 4, end: 20, quote: 'Kavvik' },
      match: { status: 'ambiguous', candidates: [{ key: 'S-1025', title: 'Kavvik' }] },
    });
  });

  it('posts decisions to items:decide and maps the materialized counts', async () => {
    const decided = {
      proposal_id: 'prp_1',
      title: 'Extract',
      checks: { extraction: { items: 1 } },
      items: [{ ...itemOut, decision: 'accepted' }],
      materialized: { revision: 'r1', changes: 2, applied: 1, skipped: {} },
    };
    const { api, calls } = fakeKeeper({
      'POST /domains/supply/proposals/prp_1/items:decide': decided,
    });
    const result = await api.proposals.decideItems('supply', 'prp_1', {
      decisions: [{ item: 'pit_1', decision: 'accepted', edits: { to: 'S-1026' } }],
      where: { type: ['link'], min_confidence: 0.5 },
      decision: 'rejected',
    });
    expect(calls[0]?.body).toEqual({
      decisions: [{ item: 'pit_1', decision: 'accepted', edits: { to: 'S-1026' } }],
      where: { type: ['link'], min_confidence: 0.5 },
      decision: 'rejected',
    });
    expect(result.materialized).toEqual(decided.materialized);
    expect(result.items[0]?.decision).toBe('accepted');
  });

  it('keeps the 422 issues of a branch that rejects the accepted items', async () => {
    const { api } = fakeKeeper({
      'POST /domains/supply/proposals/prp_1/items:decide': problem(
        422,
        'validation-failed',
        'No.',
        {
          issues: [
            {
              code: 'link.required',
              message: 'C-3025 needs with_supplier',
              path: ['items', 'pit_1'],
            },
          ],
        },
      ),
    });
    await expect(
      api.proposals.decideItems('supply', 'prp_1', {
        decisions: [{ item: 'pit_1', decision: 'accepted' }],
      }),
    ).rejects.toMatchObject({ status: 422, issues: [{ code: 'link.required' }] });
  });
});
