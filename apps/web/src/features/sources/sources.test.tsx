import { EditorView } from '@codemirror/view';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { KeeperApi } from '@/api';
import { problem } from '@/api/errors';
import { asRemote, createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';
import { changesLabel, lastRunOf, mappingSummary, policyLabel } from './source-summary';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

describe('Sources screen', () => {
  it('lists the Sources of the schema with mapping, policy, schedule and last run', async () => {
    renderApp('/supply/sources');
    expect(await screen.findByRole('heading', { name: 'Sources', level: 1 })).toBeInTheDocument();

    const table = await screen.findByRole('table', { name: 'Sources' });
    const row = (await within(table).findByText('erp.vendors')).closest('tr');
    if (row === null) throw new Error('row not found');
    expect(within(row).getByText('secret://env/ERP_DSN')).toBeInTheDocument();
    expect(within(row).getByText('postgres_table')).toBeInTheDocument();
    expect(within(row).getByText('3 Attributes into Supplier, key vendor_id')).toBeInTheDocument();
    expect(within(row).getByText('snapshot, auto_with_authority')).toBeInTheDocument();
    expect(within(row).getByText('0 2 * * *')).toBeInTheDocument();
    expect(await within(row).findByText('succeeded')).toBeInTheDocument();
    expect(within(row).getByText('2026-10-19 02:00 UTC')).toBeInTheDocument();
    expect(within(row).getByText('31 rows')).toBeInTheDocument();
    expect(within(row).getByText('+1 ~5 -0')).toBeInTheDocument();
  });

  it('shows the run history with the error of a failed run and a link to the Proposal', async () => {
    renderApp('/supply/sources');
    const history = await screen.findByRole('table', { name: 'Run history' });
    const rows = within(history).getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0]?.textContent)).toEqual([
      'run_7',
      'run_6',
      'run_5',
    ]);
    expect(within(history).getByText(/timed out after 5 s/)).toBeInTheDocument();
    const link = within(history).getByRole('link', { name: '#40' });
    expect(link).toHaveAttribute('href', expect.stringContaining('/supply/review/40'));
  });

  it('opens the Proposal a run created in Review', async () => {
    const user = userEvent.setup();
    renderApp('/supply/sources');
    const history = await screen.findByRole('table', { name: 'Run history' });
    await user.click(within(history).getByRole('link', { name: '#40' }));
    expect(
      await screen.findByRole('heading', { name: /Sync erp.vendors run 6/ }),
    ).toBeInTheDocument();
  });

  it('syncs a Source on demand and adds the run to the history', async () => {
    const user = userEvent.setup();
    renderApp('/supply/sources');
    await screen.findByRole('table', { name: 'Run history' });
    await user.click(screen.getByRole('button', { name: 'Sync erp.vendors now' }));
    expect(await screen.findByText('Sync of erp.vendors queued (job 101).')).toBeInTheDocument();
    const history = screen.getByRole('table', { name: 'Run history' });
    await waitFor(() => {
      expect(within(history).getByText('run_101')).toBeInTheDocument();
    });
    expect(within(history).getByText('manual')).toBeInTheDocument();
  });

  it('points to the data wizard when the Branch has no Sources', async () => {
    renderApp('/hr/sources');
    const empty = await screen.findByText(/This Branch has no Sources/);
    expect(within(empty).getByRole('link', { name: 'Modeler' })).toHaveAttribute(
      'href',
      expect.stringContaining('view=start'),
    );
    expect(await screen.findByText(/No runs yet/)).toBeInTheDocument();
  });

  it('keeps the list from the schema when the runs cannot be read', async () => {
    const mock = asRemote(createMockApi(options), 0);
    const forbidden = () =>
      Promise.reject(problem(403, 'Forbidden', 'needs a Domain-wide grant', 'forbidden'));
    const api: KeeperApi = {
      ...mock,
      sources: { ...mock.sources, runs: forbidden },
    };
    renderApp('/supply/sources', testConfig(), api);

    const table = await screen.findByRole('table', { name: 'Sources' });
    expect(within(table).getByText('erp.vendors')).toBeInTheDocument();
    expect(await within(table).findByText('n/a')).toBeInTheDocument();
    expect(within(table).getByRole('button', { name: 'Sync erp.vendors now' })).toBeEnabled();
    expect(screen.queryByRole('table', { name: 'Run history' })).not.toBeInTheDocument();
  });

  it('edits the Sources as YAML, lists the change and validates with the schema rules', async () => {
    const user = userEvent.setup();
    renderApp('/supply/sources?branch=cleanup%2Ftiers');
    const editor = await screen.findByRole('group', { name: 'Sources YAML editor' });
    await waitFor(() => {
      expect(editor.querySelector('.cm-content')?.textContent).toContain('sources:');
    });
    expect(screen.getByText(/The mapping editor is planned/)).toBeInTheDocument();
    expect(screen.getByText('No pending schema changes on cleanup/tiers.')).toBeInTheDocument();

    typeInYaml(editor, '0 2 * * *', '0 3 * * *');
    const change = await screen.findByRole('region', { name: 'Change set' });
    expect(
      await within(change).findByText(/update source erp.vendors \(schedule\)/),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(within(change).getByRole('button', { name: 'Save change set' })).toBeEnabled();
    });

    typeInYaml(editor, 'secret://env/ERP_DSN', 'postgres://user:pw@db/erp');
    expect(await screen.findByText(/secret reference/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(within(change).getByRole('button', { name: 'Save change set' })).toBeDisabled();
    });

    typeInYaml(editor, 'sources:', 'other:');
    expect(await screen.findByText(/Only the sources: section is edited here/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Reset to cleanup/tiers' }));
    expect(
      await screen.findByText('No pending schema changes on cleanup/tiers.'),
    ).toBeInTheDocument();
  });
});

function typeInYaml(host: HTMLElement, from: string, to: string) {
  const dom = host.querySelector<HTMLElement>('.cm-editor');
  const view = dom === null ? null : EditorView.findFromDOM(dom);
  if (view === null) throw new Error('The YAML editor is not mounted.');
  const index = view.state.doc.toString().indexOf(from);
  if (index < 0) throw new Error(`"${from}" is not in the YAML.`);
  view.dispatch({ changes: { from: index, to: index + from.length, insert: to } });
}

describe('source summaries', () => {
  it('summarizes the mapping, policy and counts of a run', async () => {
    const api = asRemote(createMockApi(options), 0);
    const schema = await api.schema.get({ domain: 'supply' });
    const source = schema.sources[0];
    if (source === undefined) throw new Error('fixture');
    expect(mappingSummary(source, schema)).toBe('3 Attributes into Supplier, key vendor_id');
    expect(policyLabel(source)).toBe('snapshot, auto_with_authority');

    const runs = (await api.sources.runs('supply')).items;
    expect(lastRunOf(runs, 'erp.vendors')?.id).toBe('run_7');
    expect(lastRunOf(runs, 'other')).toBeUndefined();
    const second = runs[1];
    if (second === undefined) throw new Error('fixture');
    expect(changesLabel(second)).toBe('+0 ~4 -0');
  });
});
