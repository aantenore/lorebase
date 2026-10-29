import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { testConfig } from '@/config/env';
import { messages } from '@/messages';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import type { KeeperApi } from '@/api';
import { renderApp } from '@/test/render';
import { segment, spansOf } from './document-spans';

const file = (
  name: string,
  content = '# Notice\n\nPart P-10099 is late.\n',
  type = 'text/markdown',
) => new File([content], name, { type });

async function openLibrary(config = testConfig()) {
  const user = userEvent.setup();
  const utils = renderApp('/supply/documents', config);
  const table = await screen.findByRole('table', { name: 'Documents' });
  return { user, table, ...utils };
}

describe('Documents: library', () => {
  it('lists the documents with the status of their extraction and the Proposal', async () => {
    const { table } = await openLibrary();
    expect(screen.getByRole('link', { name: 'Documents' })).toHaveAttribute('aria-current', 'page');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(7);

    const contract = rows.find((r) => /contract-c-3025/.test(r.textContent ?? ''));
    if (contract === undefined) throw new Error('no row');
    expect(await within(contract).findByText('succeeded')).toBeInTheDocument();
    expect(
      within(contract).getByRole('link', { name: 'Proposal #44' }).getAttribute('href'),
    ).toContain('/supply/review/44?');

    const scan = rows.find((r) => /scan-contract/.test(r.textContent ?? ''));
    if (scan === undefined) throw new Error('no row');
    expect(await within(scan).findByText('failed')).toBeInTheDocument();
    expect(within(scan).queryByRole('link', { name: /Proposal/ })).toBeNull();
  });
});

describe('Documents: versions in the library', () => {
  it('shows version, source key and a status badge only when not active', async () => {
    const { table } = await openLibrary();
    const rows = within(table).getAllByRole('row').slice(1);
    const notices = rows.filter((r) => /quality-notice-p-10016/.test(r.textContent ?? ''));
    expect(notices).toHaveLength(2);
    expect(notices[0]).toHaveTextContent('quality/notice-p-10016');
    expect(notices[0]).toHaveTextContent('v2');
    expect(within(notices[0] as HTMLElement).queryByText('superseded')).toBeNull();
    expect(notices[1]).toHaveTextContent('v1');
    expect(within(notices[1] as HTMLElement).getByText('superseded')).toBeInTheDocument();
  });

  it('sends the source key with an upload and reports the new version', async () => {
    const { user } = await openLibrary();
    const input = screen.getByLabelText(messages.documents.intake.inputLabel);
    await user.upload(input, file('x1.md', 'v one'));
    await user.type(screen.getByLabelText(/Source key/), 'policy/x');
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    await screen.findByText(/: Stored; extraction job/);
    await user.upload(input, file('x2.md', 'v two'));
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    expect(await screen.findByRole('list', { name: 'Upload results' })).toHaveTextContent(
      'x2.md: Stored as version 2.',
    );
  });
});

describe('Documents: lifecycle on the document page', () => {
  it('shows key, version, status, versions and the stale candidates link', async () => {
    renderApp('/supply/documents/doc_quality_notice_114_v1');
    await screen.findByRole('heading', { name: 'quality-notice-p-10016.md' });
    const details = screen.getByLabelText(messages.documents.lifecycle.details);
    expect(details).toHaveTextContent('quality/notice-p-10016');
    expect(details).toHaveTextContent('v1');
    expect(details).toHaveTextContent('superseded');
    const stale = screen.getByRole('link', { name: messages.documents.lifecycle.staleLink });
    expect(stale.getAttribute('href')).toContain('/supply/reconcile?');
    expect(decodeURIComponent(stale.getAttribute('href') ?? '')).toContain(
      'source=document:doc_quality_notice_114_v1',
    );
    expect(stale.getAttribute('href')).toContain('type=stale');

    const list = await screen.findByRole('list', {
      name: messages.documents.lifecycle.versionsLabel,
    });
    const entries = within(list).getAllByRole('listitem');
    expect(entries[0]).toHaveTextContent('v2');
    expect(within(entries[0] as HTMLElement).getByRole('link')).toBeInTheDocument();
    expect(entries[1]).toHaveTextContent('v1 (current)');
  });

  it('retires with a reason and shows it retired', async () => {
    const user = userEvent.setup();
    renderApp('/supply/documents/doc_scan_1999');
    await user.click(await screen.findByRole('button', { name: 'Retire' }));
    const dialog = screen.getByRole('dialog', { name: messages.documents.lifecycle.retireTitle });
    const confirm = within(dialog).getByRole('button', { name: 'Retire Document' });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText('Reason'), 'Superseded by a clean scan');
    await user.click(confirm);
    expect(await screen.findByText(/Retired by usr_mock on/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retire' })).toBeNull();
  });

  it('purges only after the document id is typed, then says it was purged', async () => {
    const user = userEvent.setup();
    renderApp('/supply/documents/doc_contracts_oct');
    await user.click(await screen.findByRole('button', { name: 'Purge' }));
    const dialog = screen.getByRole('dialog', { name: messages.documents.lifecycle.purgeTitle });
    const confirm = within(dialog).getByRole('button', { name: 'Purge Document' });
    await user.type(within(dialog).getByLabelText('Reason'), 'Wrong file');
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type doc_contracts_oct/), 'doc_contracts_o');
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Type doc_contracts_oct/), 'ct');
    await user.click(confirm);
    expect(await screen.findByText(/Purged by usr_mock on/)).toBeInTheDocument();
    expect(await screen.findByText(messages.documents.viewer.wasPurged)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Purge' })).toBeNull();
  });

  it('hides Retire and Purge when the caller may not', async () => {
    const base: KeeperApi = asRemote(
      createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }),
      0,
    );
    const api: KeeperApi = {
      ...base,
      documents: {
        ...base.documents,
        get: async (domain, id) => ({
          ...(await base.documents.get(domain, id)),
          can_retire: false,
          can_purge: false,
        }),
      },
    };
    renderApp('/supply/documents/doc_scan_1999', testConfig(), api);
    await screen.findByRole('heading', { name: 'scan-contract-1999.pdf' });
    expect(screen.queryByRole('button', { name: 'Retire' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Purge' })).toBeNull();
  });
});

describe('Documents: intake', () => {
  it('states the caps of the configuration and refuses what breaks them', async () => {
    const config = testConfig({
      documents: { ...testConfig().documents, maxFileBytes: 1024, fileSuffixes: ['.md', '.pdf'] },
    });
    const { user } = await openLibrary(config);
    expect(screen.getByText(/Up to 20 files, 1 KiB each \(\.md, \.pdf\)/)).toBeInTheDocument();
    const input = screen.getByLabelText(messages.documents.intake.inputLabel);
    await user.upload(input, file('big.md', 'x'.repeat(2048)));
    expect(screen.getByRole('alert', { name: 'Rejected files' })).toHaveTextContent(
      'big.md: Larger than 1 KiB',
    );
    fireEvent.drop(screen.getByRole('group', { name: messages.documents.intake.zoneLabel }), {
      dataTransfer: { files: [file('notes.txt')] },
    });
    expect(screen.getByRole('alert', { name: 'Rejected files' })).toHaveTextContent(
      'notes.txt: Only .md, .pdf files are accepted.',
    );
    expect(
      screen.getByRole('button', { name: messages.documents.intake.upload(0) }),
    ).toBeDisabled();
  });

  it('uploads a document, queues its extraction and follows the job to its end', async () => {
    const config = testConfig({
      documents: { ...testConfig().documents, jobPollMs: 20 },
    });
    const { user, table } = await openLibrary(config);
    await user.upload(
      screen.getByLabelText(messages.documents.intake.inputLabel),
      file('notice-9.md'),
    );
    expect(screen.getByRole('list', { name: 'Selected files' })).toHaveTextContent('notice-9.md');
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));

    const results = await screen.findByRole('list', { name: 'Upload results' });
    expect(results).toHaveTextContent('notice-9.md: Stored; extraction job 101 queued.');
    const row = (await within(table).findByText('notice-9.md')).closest('tr');
    if (row === null) throw new Error('no row');
    // The library asks again while the job is on its way and ends on succeeded.
    expect(await within(row).findByText('succeeded')).toBeInTheDocument();
    expect(within(row).queryByRole('link', { name: /Proposal/ })).toBeNull();
  });

  it('says so when the same bytes were stored before', async () => {
    const { user } = await openLibrary();
    const input = screen.getByLabelText(messages.documents.intake.inputLabel);
    await user.upload(input, file('a.md'));
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    await screen.findByText(/Stored; extraction job/);
    await user.upload(input, file('b.md'));
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    expect(await screen.findByRole('list', { name: 'Upload results' })).toHaveTextContent(
      'b.md: Stored before; nothing new was queued.',
    );
  });

  it('sends the extraction options with the upload and can turn extraction off', async () => {
    const { user } = await openLibrary();
    await user.upload(screen.getByLabelText(messages.documents.intake.inputLabel), file('c.md'));
    await user.click(screen.getByRole('checkbox', { name: 'Extract after storing' }));
    expect(screen.getByRole('checkbox', { name: /Tier 1/ })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    expect(await screen.findByRole('list', { name: 'Upload results' })).toHaveTextContent(
      'c.md: Stored without extraction.',
    );
  });

  it('asks for a tier when extraction is on', async () => {
    const { user } = await openLibrary();
    await user.upload(screen.getByLabelText(messages.documents.intake.inputLabel), file('d.md'));
    await user.click(screen.getByRole('checkbox', { name: /Tier 0/ }));
    await user.click(screen.getByRole('checkbox', { name: /Tier 1/ }));
    expect(screen.getByText(/Pick at least one tier/)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: messages.documents.intake.upload(1) }),
    ).toBeDisabled();
  });

  it('keeps a file Keeper refuses and shows why', async () => {
    const config = testConfig({
      documents: { ...testConfig().documents, fileSuffixes: ['.md', '.exe'] },
    });
    const { user } = await openLibrary(config);
    await user.upload(
      screen.getByLabelText(messages.documents.intake.inputLabel),
      file('tool.exe'),
    );
    await user.click(screen.getByRole('button', { name: messages.documents.intake.upload(1) }));
    const results = await screen.findByRole('list', { name: 'Upload results' });
    expect(results).toHaveTextContent('tool.exe: One or more request members are invalid.');
    expect(results).toHaveTextContent('unsupported file type .exe');
    expect(screen.getByRole('list', { name: 'Selected files' })).toHaveTextContent('tool.exe');
  });

  it('fetches a document by URL from an allowed host, and shows the refusal for another', async () => {
    const { user } = await openLibrary();
    const form = screen.getByRole('form', { name: messages.documents.intake.urlForm });
    await user.type(within(form).getByLabelText('Document URL'), 'https://elsewhere.test/c.pdf');
    await user.click(within(form).getByRole('button', { name: messages.documents.intake.fetch }));
    expect(await screen.findByRole('list', { name: 'Upload results' })).toHaveTextContent(
      'the host is not in keeper.documents.url_hosts',
    );

    await user.clear(within(form).getByLabelText('Document URL'));
    await user.type(
      within(form).getByLabelText('Document URL'),
      'https://documents.example.test/contracts/c-4000.pdf',
    );
    await user.click(within(form).getByRole('button', { name: messages.documents.intake.fetch }));
    expect(await screen.findByRole('list', { name: 'Upload results' })).toHaveTextContent(
      'c-4000.pdf: Stored; extraction job 101 queued.',
    );
  });

  it('hides URL intake when the configuration turns it off', async () => {
    await openLibrary(testConfig({ documents: { ...testConfig().documents, urlIntake: false } }));
    expect(screen.queryByRole('form', { name: messages.documents.intake.urlForm })).toBeNull();
  });
});

describe('Documents: viewer', () => {
  it('shows the parsed text with the evidence highlighted by decision', async () => {
    renderApp('/supply/documents/doc_amendment_c3013');
    expect(
      await screen.findByRole('heading', { name: 'amendment-c-3013-wexrix-filtration.md' }),
    ).toBeInTheDocument();
    const text = await screen.findByLabelText(messages.documents.viewer.textLabel);
    const marks = await waitFor(() => {
      const found = within(text).getAllByText(/payment is due 30 days after invoice/);
      expect(found.length).toBeGreaterThan(0);
      return found;
    });
    expect(marks[0]?.tagName).toBe('MARK');
    expect(marks[0]).toHaveAttribute('data-tone', 'pending');
    const jobs = screen.getByRole('table', { name: 'Extraction jobs' });
    expect(within(jobs).getByText('job_44')).toBeInTheDocument();
    expect(within(jobs).getByText('5,532')).toBeInTheDocument();
  });

  it('selects the passage of an item from the list and the URL follows', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/documents/doc_amendment_c3013');
    const list = await screen.findByRole('list', { name: messages.documents.viewer.items });
    await user.click(within(list).getAllByRole('button')[0] as HTMLElement);
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ item: 'pit_03' });
    });
    const text = screen.getByLabelText(messages.documents.viewer.textLabel);
    expect(text.querySelector('mark[data-tone="active"]')).not.toBeNull();
  });

  it('opens at the offsets named in the URL', async () => {
    renderApp('/supply/documents/doc_amendment_c3013?start=1000&end=1002');
    await screen.findByLabelText(messages.documents.viewer.textLabel);
    expect(document.querySelector('mark[data-tone="active"]')).toBeNull();
  });

  it('says when a document has no text yet', async () => {
    renderApp('/supply/documents/doc_scan_1999');
    expect(await screen.findByText(messages.documents.viewer.notParsed)).toBeInTheDocument();
    expect(screen.getByText(/no text layer/)).toBeInTheDocument();
  });
});

describe('document spans', () => {
  it('cuts the text at every boundary and shares segments between overlapping spans', () => {
    const spans = [
      { item: 'a', start: 2, end: 8, decision: 'pending' as const },
      { item: 'b', start: 5, end: 10, decision: 'accepted' as const },
    ];
    expect(segment(12, spans, null)).toEqual([
      { start: 0, end: 2, items: [], tone: 'plain' },
      { start: 2, end: 5, items: ['a'], tone: 'pending' },
      { start: 5, end: 8, items: ['a', 'b'], tone: 'pending' },
      { start: 8, end: 10, items: ['b'], tone: 'accepted' },
      { start: 10, end: 12, items: [], tone: 'plain' },
    ]);
    expect(segment(12, spans, 'b').map((s) => s.tone)).toEqual([
      'plain',
      'pending',
      'active',
      'active',
      'plain',
    ]);
  });

  it('clips spans to the text and leaves out other documents', () => {
    const item = (id: string, document: string, start: number, end: number) =>
      ({
        id,
        decision: 'pending',
        evidence: { document_id: document, chunk: 0, start, end, quote: '' },
      }) as never;
    expect(
      spansOf([item('a', 'd1', 4, 99), item('b', 'd2', 0, 3), item('c', 'd1', 7, 7)], 'd1', 10),
    ).toEqual([{ item: 'a', start: 4, end: 10, decision: 'pending' }]);
  });
});
