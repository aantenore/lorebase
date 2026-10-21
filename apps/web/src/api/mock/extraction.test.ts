import type { KeeperApi } from '../client';
import { ValidationError } from '../errors';
import { asRemote, createMockApi } from './adapter';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };
const supply = { domain: 'supply' };

/** `expect.stringContaining` typed as the string it stands for. */
const contains = (text: string): string => expect.stringContaining(text) as string;

function setup(): KeeperApi {
  return asRemote(createMockApi(options), 0);
}

const items = async (api: KeeperApi, proposal = '44', params = {}) =>
  (await api.proposals.items('supply', proposal, params)).items;

describe('mock extraction items', () => {
  it('holds the output of docs/extraction.md for the first two items', async () => {
    const [record, link] = await items(setup());
    expect(record).toMatchObject({
      type: 'record',
      decision: 'pending',
      confidence: 0.425,
      kind: 'Contract',
      key: 'C-3025',
      match: { status: 'new', complete: true },
      values: {
        title: 'Framework Supply Agreement C-3025',
        start_date: '2024-02-08',
        expiry_date: '2026-02-07',
        annual_value: '1596000.00',
        currency: 'EUR',
        incoterm: 'DDP',
        payment_terms_days: '60',
      },
    });
    expect(link).toMatchObject({
      type: 'link',
      confidence: 0.6,
      relation: 'with_supplier',
      from: 'C-3025',
      to: 'S-1025',
      evidence: { quote: contains('supplier code S-1025') },
    });
  });

  it('keeps every offset on its quote in the document text', async () => {
    const api = setup();
    for (const proposal of ['42', '44']) {
      for (const item of await items(api, proposal)) {
        const evidence = item.evidence;
        if (evidence === null) throw new Error(`${item.id} has no evidence`);
        const text = await api.documents.text('supply', evidence.document_id);
        expect(text?.slice(evidence.start, evidence.end)).toBe(evidence.quote);
      }
    }
  });

  it('filters like Keeper: status, Kind or Relation, type, and a confidence range', async () => {
    const api = setup();
    expect(await items(api, '44', { status: ['accepted'] })).toHaveLength(1);
    expect((await items(api, '44', { type: ['link'] })).map((i) => i.id)).toEqual([
      'pit_02',
      'pit_05',
      'pit_06',
      'pit_07',
      'pit_09',
    ]);
    expect(await items(api, '44', { kind: ['feeds'] })).toHaveLength(2);
    expect(await items(api, '44', { kind: ['Supplier'], type: ['value'] })).toHaveLength(4);
    const ranged = await items(api, '44', { min_confidence: 0.6, max_confidence: 0.78 });
    expect(ranged.map((i) => i.confidence)).toEqual([0.6, 0.62]);
  });

  it('pages the items in proposal order', async () => {
    const api = setup();
    const first = await api.proposals.items('supply', '44', { limit: 5 });
    expect(first.items.map((i) => i.seq)).toEqual([1, 2, 3, 4, 5]);
    const second = await api.proposals.items('supply', '44', {
      limit: 5,
      cursor: first.next_cursor,
    });
    expect(second.items.map((i) => i.seq)).toEqual([6, 7, 8, 9, 10]);
    expect(second.next_cursor).not.toBeNull();
  });

  it('has none for a Proposal that is not an extraction', async () => {
    expect(await items(setup(), '43')).toEqual([]);
  });

  it('refuses a Contract without its Link and changes nothing', async () => {
    const api = setup();
    const error = await api.proposals
      .decideItems('supply', '44', { decisions: [{ item: 'pit_01', decision: 'accepted' }] })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ValidationError);
    expect((error as ValidationError).issues).toEqual([
      expect.objectContaining({
        code: 'link.required',
        path: ['items', 'pit_01'],
        message: contains('Contract/C-3025 needs a with_supplier Link'),
      }),
    ]);
    expect((await items(api))[0]?.decision).toBe('pending');
    const branch = await api.records.list({ ...supply, branch: 'extract/job-44' }, 'Contract');
    expect(branch.rows.map((r) => r.key)).not.toContain('C-3025');
  });

  it('re-materializes the extraction Branch with exactly the accepted items', async () => {
    const api = setup();
    const result = await api.proposals.decideItems('supply', '44', {
      decisions: [
        { item: 'pit_01', decision: 'accepted' },
        { item: 'pit_02', decision: 'accepted' },
        { item: 'pit_11', decision: 'accepted' },
      ],
    });
    expect(result.materialized).toMatchObject({ changes: 4, applied: 4, skipped: {} });
    expect(result.materialized.revision).not.toBeNull();
    expect(result.checks.extraction?.decisions).toEqual({ pending: 8, accepted: 4, rejected: 1 });
    expect(result.items.map((i) => i.id)).toEqual(['pit_01', 'pit_02', 'pit_11']);

    const scope = { ...supply, branch: 'extract/job-44' };
    const contract = await api.records.get(scope, 'Contract', 'C-3025');
    expect(contract.record.attributes).toMatchObject({ currency: 'EUR', incoterm: 'DDP' });
    const supplier = await api.records.get(scope, 'Supplier', 'S-1025');
    expect(supplier.record.attributes['risk_score']).toBe('74');

    // Reject the Link again: the Contract alone cannot be on the Branch.
    await expect(
      api.proposals.decideItems('supply', '44', {
        decisions: [{ item: 'pit_02', decision: 'rejected' }],
      }),
    ).rejects.toBeInstanceOf(ValidationError);
    const undone = await api.proposals.decideItems('supply', '44', {
      decisions: [
        { item: 'pit_01', decision: 'pending' },
        { item: 'pit_02', decision: 'pending' },
      ],
    });
    expect(undone.materialized.changes).toBe(2);
    await expect(api.records.get(scope, 'Contract', 'C-3025')).rejects.toMatchObject({
      status: 404,
    });
  });

  it('decides every item that matches a filter, and reports the counts of the check', async () => {
    const api = setup();
    const result = await api.proposals.decideItems('supply', '44', {
      where: { type: ['value'], status: ['pending'], min_confidence: 0.7 },
      decision: 'accepted',
    });
    expect(result.items.map((i) => i.id)).toEqual(['pit_11', 'pit_12']);
    const detail = await api.proposals.get('supply', '44');
    expect(detail.checks.find((c) => c.name === 'extraction')).toMatchObject({
      status: 'pending',
      message: '13 item(s): 3 accepted, 9 pending, 1 rejected.',
    });
  });

  it('applies the edits of the reviewer, and picks another Record for an ambiguous match', async () => {
    const api = setup();
    const result = await api.proposals.decideItems('supply', '44', {
      decisions: [
        { item: 'pit_06', decision: 'accepted', edits: { to: 'PL-15' } },
        { item: 'pit_03', decision: 'accepted', edits: { value: 'DDP' } },
      ],
    });
    const [incoterm, link] = result.items;
    expect(incoterm).toMatchObject({ value: 'DDP', edits: { value: 'DDP' } });
    expect(link).toMatchObject({ to: 'PL-15', edits: { to: 'PL-15' } });
    const scope = { ...supply, branch: 'extract/job-44' };
    expect((await api.records.get(scope, 'Contract', 'C-3013')).record.attributes['incoterm']).toBe(
      'DDP',
    );
    const links = await api.records.links(scope, 'Supplier', 'S-1013', 'feeds');
    expect(links.links.map((l) => l.ref)).toEqual(['Plant/PL-15']);

    // An empty edit clears the stored ones.
    const cleared = await api.proposals.decideItems('supply', '44', {
      decisions: [{ item: 'pit_06', decision: 'accepted', edits: {} }],
    });
    expect(cleared.items[0]).toMatchObject({ to: 'PL-14', edits: null });
  });

  it('checks edits against the schema and the item type', async () => {
    const api = setup();
    const refuse = async (input: Parameters<KeeperApi['proposals']['decideItems']>[2]) =>
      api.proposals.decideItems('supply', '44', input).catch((e: unknown) => e as ValidationError);
    const badValue = await refuse({
      decisions: [{ item: 'pit_01', decision: 'accepted', edits: { values: { currency: 'eur' } } }],
    });
    expect(badValue).toBeInstanceOf(ValidationError);
    expect((badValue as ValidationError).fields[0]).toMatchObject({
      pointer: '/body/decisions/0/edits/values/currency',
    });
    const wrongType = await refuse({
      decisions: [{ item: 'pit_03', decision: 'accepted', edits: { to: 'x' } }],
    });
    expect((wrongType as ValidationError).fields[0]?.message).toContain('value items accept edits');
    const unknown = await refuse({ decisions: [{ item: 'nope', decision: 'accepted' }] });
    expect((unknown as ValidationError).fields[0]?.message).toBe('unknown item nope');
    expect(await refuse({ decisions: [] })).toBeInstanceOf(ValidationError);
  });

  it('shows why an accepted item is not on the Branch', async () => {
    const api = setup();
    // The part is not accepted, so the Link to it has nothing to attach to.
    const result = await api.proposals.decideItems('supply', '44', {
      decisions: [{ item: 'pit_09', decision: 'accepted' }],
    });
    expect(result.materialized.skipped).toEqual({
      pit_09: 'Part/P-10016 is not on the branch; accept its Record too.',
    });
    expect(result.items[0]?.not_applied).toContain('accept its Record too');
    const listed = await items(api);
    expect(listed.find((i) => i.id === 'pit_09')?.not_applied).toContain('P-10016');
  });

  it('refuses decisions on a closed Proposal', async () => {
    const api = setup();
    await api.proposals.close('supply', '44');
    await expect(
      api.proposals.decideItems('supply', '44', {
        decisions: [{ item: 'pit_11', decision: 'accepted' }],
      }),
    ).rejects.toMatchObject({ status: 409 });
  });
});

describe('mock documents', () => {
  it('lists the library newest first and pages it', async () => {
    const api = setup();
    const first = await api.documents.list('supply', { limit: 2 });
    expect(first.items.map((d) => d.id)).toEqual(['doc_contract_c3025', 'doc_amendment_c3013']);
    const rest = await api.documents.list('supply', { cursor: first.next_cursor });
    expect(rest.items).toHaveLength(4);
    expect(rest.next_cursor).toBeNull();
    expect((await api.documents.list('hr')).items).toEqual([]);
  });

  it('reads a document with the job that opened Proposal 44', async () => {
    const detail = await setup().documents.get('supply', 'doc_quality_notice_114');
    expect(detail.extraction_jobs[0]).toMatchObject({
      id: 'job_44',
      status: 'succeeded',
      proposal_id: '44',
      branch: 'extract/job-44',
    });
  });

  it('refuses an unknown document, a bad tier and an oversized one', async () => {
    const api = setup();
    await expect(api.documents.get('supply', 'doc_x')).rejects.toMatchObject({ status: 404 });
    await expect(
      api.documents.upload('supply', { file: new File(['x'], 'a.md'), tiers: [3] }),
    ).rejects.toMatchObject({ status: 422 });
    await expect(
      api.documents.upload('supply', { file: new File([], 'a.md') }),
    ).rejects.toMatchObject({ status: 422 });
    const big = new File(['x'], 'big.md');
    Object.defineProperty(big, 'size', { value: 30_000_000 });
    await expect(api.documents.upload('supply', { file: big })).rejects.toMatchObject({
      status: 413,
    });
  });

  it('keeps the text of a Markdown upload for the viewer', async () => {
    const api = setup();
    const { document } = await api.documents.upload('supply', {
      file: new File(['# Hello'], 'hello.md'),
      extract: false,
    });
    expect(await api.documents.text('supply', document.id)).toBe('# Hello');
    expect((await api.documents.get('supply', document.id)).extraction_jobs).toEqual([]);
  });
});
