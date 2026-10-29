import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { KeeperApi, HistoryResponse, RecordChange } from '@/api';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

const mock = () => asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);

const change = (revision: string, message: string, overrides: Partial<RecordChange> = {}) => ({
  revision,
  generation: 1,
  at: '2026-10-02T10:30:00Z',
  author: 'svc_scribe',
  on_behalf_of: null,
  message,
  origin: {},
  change: 'updated' as const,
  attributes: [{ name: 'status', before: 'draft', after: 'active', provenance: null }],
  provenance: null,
  ...overrides,
});

const historyPage = (items: RecordChange[], next: string | null): HistoryResponse => ({
  revision: 'r9',
  served_from: 'tree',
  branch: 'main',
  kind: 'Part',
  key: 'P-77813',
  items,
  next_cursor: next,
});

describe('Explorer', () => {
  it('shows the Domain home with Kinds, Revisions and open Proposals', async () => {
    renderApp('/supply/explorer');
    expect(await screen.findByRole('heading', { name: 'Explorer', level: 1 })).toBeInTheDocument();
    const kinds = await screen.findByRole('heading', { name: 'Kinds' });
    expect(kinds).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: /Supplier/ })).toBeInTheDocument();
    expect(await screen.findByText('Sync erp.vendors run 7')).toBeInTheDocument();
    expect(
      await screen.findByRole('link', { name: /Contracts batch October/ }),
    ).toBeInTheDocument();
  });

  it('searches Records and filters by Kind', async () => {
    const user = userEvent.setup();
    renderApp('/supply/explorer');
    const box = await screen.findByLabelText('Search Records');
    await user.type(box, 'brevik{Enter}');

    const results = await screen.findByRole('table', { name: 'Search results' });
    expect(
      within(results).getByRole('link', { name: 'Brevik Bearings S.p.A.' }),
    ).toBeInTheDocument();
    expect(
      within(results).getByRole('link', { name: 'Brevik master supply agreement' }),
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Kind'), 'Supplier');
    await screen.findByRole('link', { name: 'Brevik Bearings S.p.A.' });
    expect(
      screen.queryByRole('link', { name: 'Brevik master supply agreement' }),
    ).not.toBeInTheDocument();
  });

  it('searches the Kinds of a Trait, or of a lens when no Kind or Trait is chosen', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer?q=brevik');
    await screen.findByRole('table', { name: 'Search results' });

    await user.selectOptions(screen.getByLabelText('Kind'), 'Trait: Located');
    await screen.findByRole('link', { name: 'Brevik Bearings S.p.A.' });
    expect(
      screen.queryByRole('link', { name: 'Brevik master supply agreement' }),
    ).not.toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ trait: 'Located' });

    await user.selectOptions(screen.getByLabelText('Kind'), 'All Kinds');
    await user.selectOptions(screen.getByLabelText('Lens'), 'Supplier risk');
    expect(
      await screen.findByRole('link', { name: 'Brevik master supply agreement' }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Lens')).toHaveAccessibleDescription(
      'A supplier with its contracts and parts.',
    );
    await user.selectOptions(screen.getByLabelText('Lens'), 'Sites');
    await screen.findByRole('link', { name: 'Brevik Bearings S.p.A.' });
    expect(
      screen.queryByRole('link', { name: 'Brevik master supply agreement' }),
    ).not.toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ lens: 'sites' });
  });

  it('offers no lens or Trait when the schema declares none', async () => {
    renderApp('/hr/explorer');
    const kind = await screen.findByLabelText('Kind');
    expect(within(kind).queryByRole('option', { name: /^Trait:/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Lens')).not.toBeInTheDocument();
  });

  it('lists the Records of a Kind when no text is typed', async () => {
    const user = userEvent.setup();
    renderApp('/supply/explorer');
    await user.selectOptions(await screen.findByLabelText('Kind'), 'Part');
    const table = await screen.findByRole('table', { name: 'Part Records' });
    expect(within(table).getByText('Deep groove bearing 6204')).toBeInTheDocument();
  });

  it('opens a Record page with derived values, linked Records and Lineage', async () => {
    renderApp('/supply/explorer/Part/P-77813');
    expect(
      await screen.findByRole('heading', { name: 'Tapered roller bearing' }),
    ).toBeInTheDocument();
    const attributes = await screen.findByRole('table', { name: 'Attributes' });
    expect(within(attributes).getByText('derived')).toBeInTheDocument();
    expect(await screen.findByRole('region', { name: 'supplied_by links' })).toHaveTextContent(
      'Brevik Bearings S.p.A.',
    );
    expect(await screen.findByRole('table', { name: 'Lineage of each value' })).toBeInTheDocument();
  });

  it('pages and sorts a Kind table through the URL', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer/Supplier');
    const table = await screen.findByRole('table', { name: 'Supplier Records' });
    await user.click(within(table).getByRole('link', { name: 'Risk score' }));
    await screen.findByRole('link', { name: 'Risk score (asc)' });
    expect(router.state.location.search).toMatchObject({ sort: 'risk_score', dir: 'asc' });
  });

  it('shows origin, Source, extraction evidence, Revision and author per value in Lineage', async () => {
    renderApp('/supply/explorer/Part/P-77813');
    const table = await screen.findByRole('table', { name: 'Lineage of each value' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows[0]).toHaveTextContent('source_sync');
    expect(rows[0]).toHaveTextContent('Source erp.vendors');
    expect(rows[0]).toHaveTextContent('usr_marco');
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent),
    ).toEqual(['Attribute', 'Origin', 'Value', 'Detail', 'Revision', 'Author', 'At']);
  });

  it('lists the changes in the History tab and loads more through the cursor', async () => {
    const user = userEvent.setup();
    const base = mock();
    const cursors: (string | null | undefined)[] = [];
    const api: KeeperApi = {
      ...base,
      records: {
        ...base.records,
        history: (_scope, _kind, _key, params) => {
          cursors.push(params?.cursor);
          expect(params?.values).toBe(true);
          return Promise.resolve(
            params?.cursor === 'c2'
              ? historyPage([change('bbbbbbbb2222', 'Import parts', { change: 'created' })], null)
              : historyPage([change('aaaaaaaa1111', 'Raise status')], 'c2'),
          );
        },
      },
    };
    renderApp('/supply/explorer/Part/P-77813', testConfig(), api);
    await user.click(await screen.findByRole('tab', { name: 'History' }));

    const list = await screen.findByRole('list', { name: 'Changes, newest first' });
    expect(list).toHaveTextContent('updated');
    expect(list).toHaveTextContent('aaaaaaaa');
    expect(list).toHaveTextContent('Raise status');
    expect(list).toHaveTextContent('svc_scribe');
    expect(list).toHaveTextContent('2026-10-02 10:30 UTC');
    expect(list).toHaveTextContent('status: draft');
    expect(list).toHaveTextContent('active');

    await user.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('Import parts');
    expect(screen.getByText('created')).toBeInTheDocument();
    expect(screen.getByText('Raise status')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
    expect(cursors).toEqual([null, 'c2']);
  });
});
