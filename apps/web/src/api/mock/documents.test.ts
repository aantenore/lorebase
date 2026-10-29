import type { KeeperApi } from '../client';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

const setup = (): KeeperApi['documents'] => asRemote(createMockApi(options), 0).documents;

const file = (content: string, name = 'policy.md') =>
  new File([content], name, { type: 'text/markdown' });

describe('mock documents: versions and lifecycle', () => {
  it('ignores the same bytes under a key and supersedes with new bytes', async () => {
    const api = setup();
    const first = await api.upload('supply', { file: file('one'), source_key: 'policy/a' });
    expect(first.duplicate).toBe(false);
    expect(first.document).toMatchObject({ source_key: 'policy/a', version: 1, status: 'active' });

    const same = await api.upload('supply', {
      file: file('one', 'copy.md'),
      source_key: 'policy/a',
    });
    expect(same.duplicate).toBe(true);
    expect(same.document.id).toBe(first.document.id);

    const next = await api.upload('supply', { file: file('two'), source_key: 'policy/a' });
    expect(next.duplicate).toBe(false);
    expect(next.document).toMatchObject({ version: 2, replaces: first.document.id });
    expect((await api.get('supply', first.document.id)).status).toBe('superseded');

    const versions = await api.versions('supply', next.document.id);
    expect(versions.items.map((v) => v.version)).toEqual([2, 1]);
  });

  it('lists the seeded key with two versions', async () => {
    const api = setup();
    const versions = await api.versions('supply', 'doc_quality_notice_114');
    expect(versions.items.map((v) => [v.version, v.status])).toEqual([
      [2, 'active'],
      [1, 'superseded'],
    ]);
  });

  it('retires a document, idempotently', async () => {
    const api = setup();
    const done = await api.retire('supply', 'doc_scan_1999', 'Obsolete');
    expect(done.document).toMatchObject({ status: 'retired', retired_by: 'usr_mock' });
    const again = await api.retire('supply', 'doc_scan_1999', 'Obsolete');
    expect(again.document.retired_at).toBe(done.document.retired_at);
  });

  it('purges only when the confirmation equals the id, and clears the text', async () => {
    const api = setup();
    await expect(
      api.purge('supply', 'doc_contracts_oct', { reason: 'Mistake', confirm: 'nope' }),
    ).rejects.toMatchObject({ status: 422 });
    expect(await api.text('supply', 'doc_contracts_oct')).not.toBeNull();

    const done = await api.purge('supply', 'doc_contracts_oct', {
      reason: 'Mistake',
      confirm: 'doc_contracts_oct',
    });
    expect(done).toMatchObject({ text_deleted: true, document: { status: 'purged' } });
    expect(await api.text('supply', 'doc_contracts_oct')).toBeNull();
    expect((await api.get('supply', 'doc_contracts_oct')).text_sha256).toBeNull();
  });
});
