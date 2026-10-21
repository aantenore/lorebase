import { parse } from 'yaml';
import type { KeeperApi } from '../client';
import { ApiError } from '../errors';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

function setup(): KeeperApi {
  return asRemote(createMockApi(options), 0);
}

const csv = (name: string) => new File(['a\n1\n'], name);

describe('mock inference', () => {
  it('answers with the recorded proposal for the supply tables', async () => {
    const proposal = await setup().inference.infer('supply', {
      files: [
        'contract',
        'delivered_to',
        'flags',
        'for_part',
        'part',
        'plant',
        'purchase_order',
        'risk_flag',
        'stocked_at',
        'supplied_by',
        'supplier',
        'with_supplier',
      ].map((t) => csv(`${t}.csv`)),
    });
    const document = parse(proposal.yaml) as {
      kinds: Record<string, unknown>;
      relations: Record<string, unknown>;
    };
    expect(Object.keys(document.kinds)).toHaveLength(6);
    expect(Object.keys(document.relations)).toHaveLength(7);
    expect(proposal.tables).toHaveLength(12);
    expect(proposal.issues.map((i) => i.code)).toEqual(['infer.ambiguous_primary_key']);
    // One evidence entry per element, 63 in the recorded run, each tied to an id of the document.
    expect(proposal.evidence).toHaveLength(63);
    expect(proposal.evidence.every((e) => e.element_id !== 'undefined' && e.confidence > 0)).toBe(
      true,
    );
    expect(proposal.evidence.find((e) => e.element === 'relations.supplied_by')?.confidence).toBe(
      0.85,
    );
  });

  it('narrows the proposal to the tables it is given and drops what dangles', async () => {
    const proposal = await setup().inference.infer('supply', {
      files: [csv('purchase_order.csv'), csv('part.csv'), csv('for_part.csv')],
    });
    const document = parse(proposal.yaml) as {
      kinds: Record<string, { attributes: Record<string, { type: unknown }> }>;
      relations: Record<string, unknown>;
    };
    expect(Object.keys(document.kinds).sort()).toEqual(['Part', 'PurchaseOrder']);
    // supplier is no longer a Kind, so the Attribute that referenced it is plain text again.
    expect(document.kinds['PurchaseOrder']?.attributes['supplier']?.type).toBe('string');
    expect(Object.keys(document.relations)).toEqual(['for_part']);
    expect(proposal.evidence.some((e) => e.element.startsWith('kinds.Supplier'))).toBe(false);
    expect(proposal.issues).toEqual([]);
    expect(proposal.issue_count).toBe(0);
  });

  it('refuses unknown tables, empty uploads and sources that are not allowlisted', async () => {
    const api = setup();
    await expect(api.inference.infer('supply', { files: [] })).rejects.toMatchObject({
      status: 422,
    });
    await expect(api.inference.infer('supply', { files: [csv('vendors.csv')] })).rejects.toThrow(
      /unknown: vendors/,
    );
    await expect(
      api.inference.infer('supply', { postgres: { dsn: 'secret://env/OTHER' } }),
    ).rejects.toBeInstanceOf(ApiError);
    await expect(
      api.inference.infer('nowhere', { files: [csv('plant.csv')] }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it('reads an allowlisted Postgres source and audits the inference', async () => {
    const api = setup();
    const proposal = await api.inference.infer('supply', {
      postgres: { dsn: 'secret://env/ERP_DSN', tables: ['plant'] },
    });
    expect(Object.keys((parse(proposal.yaml) as { kinds: object }).kinds)).toEqual(['Plant']);
    const audit = await api.admin.audit.list('supply', { limit: 1 });
    expect(audit.items[0]).toMatchObject({ action: 'schema.infer', target: 'postgres' });
  });
});

describe('mock Sources', () => {
  it('lists the runs newest first and filters by Source', async () => {
    const api = setup();
    const runs = await api.sources.runs('supply');
    expect(runs.items.map((r) => [r.id, r.status])).toEqual([
      ['run_7', 'succeeded'],
      ['run_6', 'succeeded'],
      ['run_5', 'failed'],
    ]);
    expect((await api.sources.runs('supply', { source: 'nope' })).items).toEqual([]);
    expect((await api.sources.runs('hr')).items).toEqual([]);
    const page = await api.sources.runs('supply', { limit: 2 });
    expect(page.next_cursor).not.toBeNull();
  });

  it('runs a Source on demand and records the run and its audit entry', async () => {
    const api = setup();
    const run = await api.sources.sync('supply', 'erp.vendors');
    expect(run).toMatchObject({ source: 'erp.vendors', status: 'succeeded', trigger: 'manual' });
    expect((await api.sources.runs('supply')).items[0]?.id).toBe(run.id);
    expect((await api.admin.audit.list('supply', { limit: 1 })).items[0]?.action).toBe(
      'source.sync',
    );
    await expect(api.sources.sync('supply', 'missing')).rejects.toMatchObject({ status: 404 });
  });
});

describe('mock administration', () => {
  it('shows the secret of a new API key once, with a service principal and a grant', async () => {
    const api = setup();
    const created = await api.admin.apiKeys.create('supply', {
      name: 'Report builder',
      role: 'viewer',
      ttl_days: 10,
    });
    expect(created.key).toContain(created.prefix);
    expect(created.role).toBe('viewer');

    const listed = (await api.admin.apiKeys.list('supply')).items.find((k) => k.id === created.id);
    expect(listed).toBeDefined();
    expect(listed).not.toHaveProperty('key');

    const principals = await api.admin.principals.list({ type: 'service' });
    expect(principals.items.map((p) => p.display_name)).toContain('Report builder');
    const grants = await api.admin.grants.list('supply');
    expect(
      grants.items.some((g) => g.subject === created.principal_id && g.role === 'viewer'),
    ).toBe(true);
  });

  it('revokes a key once and keeps a chained audit trail', async () => {
    const api = setup();
    await api.admin.apiKeys.revoke('supply', 'key_01');
    const keys = (await api.admin.apiKeys.list('supply')).items;
    expect(keys.find((k) => k.id === 'key_01')?.revoked_at).not.toBeNull();
    await expect(api.admin.apiKeys.revoke('supply', 'key_01')).rejects.toMatchObject({
      status: 409,
    });
    await expect(api.admin.apiKeys.revoke('supply', 'nope')).rejects.toMatchObject({ status: 404 });

    const audit = (await api.admin.audit.list('supply')).items;
    expect(audit[0]).toMatchObject({ action: 'apikey.revoke', target: 'key_01' });
    expect(audit.map((e) => e.seq)).toEqual([...audit.map((e) => e.seq)].sort((a, b) => b - a));
    expect(new Set(audit.map((e) => e.entry_hash)).size).toBe(audit.length);
    expect(audit.every((e) => /^[0-9a-f]{64}$/.test(e.entry_hash))).toBe(true);
  });

  it('pages the audit log newest first', async () => {
    const api = setup();
    const first = await api.admin.audit.list('supply', { limit: 4 });
    expect(first.items).toHaveLength(4);
    const second = await api.admin.audit.list('supply', { limit: 4, cursor: first.next_cursor });
    expect(second.items[0]?.seq).toBeLessThan(first.items[3]?.seq ?? 0);
  });
});
