import type { KeeperApi } from '../client';
import { ApiError } from '../errors';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };
const supply = { domain: 'supply' };

function setup(): KeeperApi {
  return asRemote(createMockApi(options), 0);
}

describe('mock adapter', () => {
  it('lists the sample Domains and their Branches with ahead and behind counts', async () => {
    const api = setup();
    const domains = await api.domains.list();
    expect(domains.items.map((d) => d.id)).toEqual(['supply', 'hr']);

    const branches = await api.branches.list('supply');
    const byName = Object.fromEntries(branches.items.map((b) => [b.name, b]));
    expect(byName['main']).toMatchObject({ protected: true, behind_default: 0, ahead_default: 0 });
    expect(byName['model/risk']).toMatchObject({ behind_default: 1, ahead_default: 2 });
  });

  it('reads the schema per Branch', async () => {
    const api = setup();
    const main = await api.schema.get(supply);
    const risk = await api.schema.get({ ...supply, branch: 'model/risk' });
    expect(main.kinds.map((k) => k.api_name)).toEqual(['Supplier', 'Part', 'Plant', 'Contract']);
    expect(risk.kinds.map((k) => k.api_name)).toContain('RiskFlag');
    expect(risk.revision).not.toBe(main.revision);
  });

  it('filters, orders and paginates Records with the query DSL', async () => {
    const api = setup();
    const italian = await api.records.list(supply, 'Supplier', {
      where: { attr: 'country', op: 'eq', value: 'IT' },
    });
    expect(italian.rows.map((r) => r.key)).toEqual(['S-1001']);

    const first = await api.records.list(supply, 'Supplier', {
      order_by: [{ attr: 'risk_score', dir: 'desc' }],
      limit: 2,
    });
    expect(first.rows.map((r) => r.key)).toEqual(['S-1003', 'S-1006']);
    expect(first.next_cursor).not.toBeNull();
    const second = await api.records.list(supply, 'Supplier', {
      order_by: [{ attr: 'risk_score', dir: 'desc' }],
      limit: 2,
      cursor: first.next_cursor,
    });
    expect(second.rows.map((r) => r.key)).toEqual(['S-1004', 'S-1001']);
  });

  it('supports via and derived predicates', async () => {
    const api = setup();
    const result = await api.query.run(supply, {
      kind: 'Part',
      where: {
        and: [
          { derived: 'single_sourced', op: 'eq', value: true },
          { via: 'supplied_by', where: { attr: 'country', op: 'in', value: ['IT', 'PL'] } },
        ],
      },
      select: ['name'],
    });
    expect(result.rows.map((r) => r.key)).toEqual(['P-77813', 'P-90310', 'P-90311']);
    expect(Object.keys(result.rows[0]?.attributes ?? {})).toEqual(['name']);
  });

  it('searches across Kinds and honors the Kind filter', async () => {
    const api = setup();
    const all = await api.search.run(supply, { q: 'brevik' });
    expect(all.results.map((r) => r.ref)).toEqual(['Supplier/S-1001', 'Contract/C-2231']);
    const suppliersOnly = await api.search.run(supply, { q: 'brevik', kinds: ['Supplier'] });
    expect(suppliersOnly.results.map((r) => r.ref)).toEqual(['Supplier/S-1001']);
  });

  it('expands a neighborhood within depth and node caps', async () => {
    const api = setup();
    const one = await api.query.neighborhood(supply, { seeds: ['Plant/TRN'], depth: 1 });
    expect(one.nodes.map((n) => n.ref).sort()).toEqual([
      'Plant/TRN',
      'Supplier/S-1001',
      'Supplier/S-1002',
      'Supplier/S-1005',
    ]);
    const capped = await api.query.neighborhood(supply, {
      seeds: ['Plant/TRN'],
      depth: 3,
      max_nodes: 3,
    });
    expect(capped.nodes).toHaveLength(3);
    expect(capped.truncated).toBe(true);
  });

  it('diffs two Branches', async () => {
    const api = setup();
    const diff = await api.revisions.diff('supply', { from: 'main', to: 'extract/job-42' });
    expect(diff.summary['Contract']).toEqual({ added: 2, modified: 0, removed: 0 });
    expect(diff.summary['with_supplier']).toEqual({ added: 2, modified: 0, removed: 0 });
  });

  it('keeps a Proposal open while Conflicts are unresolved, then merges', async () => {
    const api = setup();
    const open = await api.proposals.merge('supply', '43');
    expect(open.status).toBe('conflicts');

    const attempt = await api.conflicts.list('supply', '43');
    const fingerprint = attempt.conflicts[0]?.fingerprint ?? '';
    await api.conflicts.resolve('supply', '43', { fingerprint, choice: 'theirs' });
    const merged = await api.proposals.merge('supply', '43');
    expect(merged.status).toBe('merged');
    const supplier = await api.records.get(supply, 'Supplier', 'S-1001');
    expect(supplier.record.attributes['tier']).toBe('1');
  });

  it('reports unknown ids as problem details', async () => {
    const api = setup();
    const error = await api.domains.get('nope').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
  });

  it('corrects a typo and honors the search tuning loosely', async () => {
    const api = setup();
    const typo = await api.search.run(supply, { q: 'brevik beerings' });
    expect(typo.corrections?.[0]).toMatchObject({ word: 'beerings' });
    expect(typo.results[0]?.ref).toBe('Supplier/S-1001');

    const capped = await api.search.run(supply, { q: 'bearing', tuning: { candidates: 1 } });
    expect(capped.results).toHaveLength(1);

    const plain = await api.search.run(supply, { q: 'bearing' });
    const vector = await api.search.run(supply, {
      q: 'bearing',
      mode: 'hybrid',
      query_vector: [0.3, 0.9],
    });
    expect(plain.mode).toBe('lexical');
    expect(vector.mode).toBe('hybrid');
  });

  it('builds a context pack for a search within a budget', async () => {
    const api = setup();
    const pack = await api.agents.context(supply, { search: 'brevik', depth: 2 });
    expect(pack.seeds[0]?.ref).toBe('Supplier/S-1001');
    expect(pack.neighbors.some((n) => n.ref === 'Plant/TRN' && n.depth === 1)).toBe(true);
    expect(Object.keys(pack.links)).toContain('feeds');
    expect(pack.budget.used_tokens).toBeLessThanOrEqual(pack.budget.limit_tokens);

    const tight = await api.agents.context(supply, {
      search: 'brevik',
      depth: 2,
      budget_tokens: 150,
    });
    expect(tight.budget.used_tokens).toBeLessThan(pack.budget.used_tokens);
    expect(tight.budget.trimmed.length).toBeGreaterThan(0);
    expect(tight.neighbors.length).toBeLessThan(pack.neighbors.length);
  });

  it('serves trace sessions and their timeline', async () => {
    const api = setup();
    const all = await api.trace.sessions();
    expect(all.enabled).toBe(true);
    expect(all.items.map((s) => s.session_id)).toEqual([
      'inferred-3f9a1c07d2b84e65',
      'sess_20261020_brevik',
    ]);
    const hr = await api.trace.sessions({ domain_id: 'hr' });
    expect(hr.items.map((s) => s.session_id)).toEqual(['inferred-3f9a1c07d2b84e65']);
    const first = await api.trace.sessions({ limit: 1 });
    expect(first.next_cursor).not.toBeNull();

    const brevik = all.items[1];
    expect(brevik).toMatchObject({ calls: 5, notes: 2, proposals: ['41'] });
    const timeline = await api.trace.timeline('sess_20261020_brevik');
    expect(timeline.steps.map((s) => s.type)).toEqual([
      'call',
      'note',
      'call',
      'call',
      'call',
      'note',
      'call',
    ]);
    await expect(api.trace.timeline('nope')).rejects.toBeInstanceOf(ApiError);
    await expect(
      api.trace.timeline('sess_20261020_brevik', { actor: 'someone_else' }),
    ).rejects.toBeInstanceOf(ApiError);
  });
});
