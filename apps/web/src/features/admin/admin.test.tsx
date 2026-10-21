import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { AuditEntry } from '@/api/admin-types';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';
import { distinct, filterAudit, NO_FILTER } from './audit-filter';

const rowOf = (table: HTMLElement, text: string) => {
  const row = within(table).getByText(text).closest('tr');
  if (row === null) throw new Error(`No row with ${text}`);
  return row;
};

describe('Admin: tabs under Settings', () => {
  it('opens the sections from the tabs and keeps the General tab by default', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings');
    expect(await screen.findByRole('table', { name: 'Domain settings' })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'API keys' }));
    expect(await screen.findByRole('table', { name: 'API keys' })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Audit log' }));
    expect(await screen.findByRole('table', { name: 'Audit log' })).toBeInTheDocument();
  });
});

describe('Admin: API keys', () => {
  it('lists the keys of the Domain with their holder, expiry, last use and status', async () => {
    renderApp('/supply/settings?section=keys');
    const table = await screen.findByRole('table', { name: 'API keys' });
    const scribe = await waitFor(() => rowOf(table, 'lbk_scr1...'));
    await waitFor(() => {
      expect(within(scribe).getByText('Scribe workers')).toBeInTheDocument();
    });
    expect(within(scribe).getByText('domain:supply')).toBeInTheDocument();
    expect(within(scribe).getByText('2026-10-19 02:04 UTC')).toBeInTheDocument();
    expect(within(scribe).getByText('active')).toBeInTheDocument();

    const revoked = rowOf(table, 'lbk_hrl2...');
    expect(within(revoked).getByText('revoked')).toBeInTheDocument();
    expect(within(revoked).queryByRole('button')).not.toBeInTheDocument();
  });

  it('creates a key and shows the secret once', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=keys');
    await screen.findByRole('table', { name: 'API keys' });

    const create = screen.getByRole('button', { name: 'Create key' });
    expect(create).toBeDisabled();
    await user.type(screen.getByLabelText('Name of the service principal'), 'Report builder');
    await user.selectOptions(screen.getByLabelText('Role in this Domain'), 'viewer');
    await user.type(screen.getByLabelText('Lifetime in days'), '30');
    await user.click(create);

    const notice = await screen.findByRole('region', { name: 'New API key' });
    expect(within(notice).getByText(/shown only once/)).toBeInTheDocument();
    const secret = within(notice).getByText(/^lbk_/).textContent ?? '';
    expect(secret.length).toBeGreaterThan(20);

    const table = screen.getByRole('table', { name: 'API keys' });
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(5);
    });
    expect(within(table).queryByText(secret)).not.toBeInTheDocument();
    expect(await within(table).findByText('Report builder')).toBeInTheDocument();

    await user.click(within(notice).getByRole('button', { name: 'I have copied it' }));
    expect(screen.queryByRole('region', { name: 'New API key' })).not.toBeInTheDocument();
  });

  it('rejects a lifetime that is not a whole number of days', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=keys');
    await screen.findByRole('table', { name: 'API keys' });
    await user.type(screen.getByLabelText('Name of the service principal'), 'Report builder');
    await user.type(screen.getByLabelText('Lifetime in days'), '0');
    expect(screen.getByText('Whole days, at least 1.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create key' })).toBeDisabled();
  });

  it('asks before revoking and then marks the key revoked', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=keys');
    await user.click(await screen.findByRole('button', { name: 'Revoke lbk_scr1' }));

    const dialog = await screen.findByRole('dialog', { name: 'Revoke API key' });
    expect(within(dialog).getByText(/cannot be undone/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revoke lbk_scr1' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Revoke lbk_scr1' }));
    await user.click(await screen.findByRole('button', { name: 'Revoke key' }));
    expect(await screen.findByText('API key lbk_scr1 revoked.')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Revoke lbk_scr1' })).not.toBeInTheDocument();
    });
    const table = screen.getByRole('table', { name: 'API keys' });
    expect(within(rowOf(table, 'lbk_scr1...')).getByText('revoked')).toBeInTheDocument();
  });
});

describe('Admin: principals and grants', () => {
  it('lists principals and filters them by type', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=access');
    const principals = await screen.findByRole('table', { name: 'Principals' });
    expect(within(principals).getByText('Marco Rossi')).toBeInTheDocument();
    expect(within(principals).getByText('Scribe workers')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Type'), 'service');
    await waitFor(() => {
      expect(
        within(screen.getByRole('table', { name: 'Principals' })).queryByText('Marco Rossi'),
      ).toBeNull();
    });
    expect(
      within(screen.getByRole('table', { name: 'Principals' })).getByText('Herald (MCP agent)'),
    ).toBeInTheDocument();
  });

  it('lists the grants of the Domain with their scope, by name', async () => {
    renderApp('/supply/settings?section=access');
    const grants = await screen.findByRole('table', { name: 'Grants' });
    await waitFor(() => {
      expect(within(grants).getAllByText('Luca Bianchi')).toHaveLength(2);
    });
    expect(within(grants).getByText('modeler')).toBeInTheDocument();
    expect(within(grants).getByText('branch source/*')).toBeInTheDocument();
    expect(within(grants).getByText('operation flag_supplier_risk')).toBeInTheDocument();
    // Read only: no way to add or remove a grant from this screen.
    expect(within(grants).queryByRole('button')).not.toBeInTheDocument();
  });
});

describe('Admin: audit log', () => {
  it('shows the newest entries first with action, target, details and hash', async () => {
    renderApp('/supply/settings?section=audit');
    const table = await screen.findByRole('table', { name: 'Audit log' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(9);
    expect(within(rows[0] as HTMLElement).getByText('source.sync')).toBeInTheDocument();
    expect(
      within(rows[0] as HTMLElement).getByText('{"run":"run_7","changes":6}'),
    ).toBeInTheDocument();
    expect(within(rows[8] as HTMLElement).getByText('domain.create')).toBeInTheDocument();
    const hash = within(rows[0] as HTMLElement).getByTitle(/^[0-9a-f]{64}$/);
    expect(hash.textContent).toHaveLength(12);
  });

  it('filters the loaded entries by actor, action and text, and clears the filters', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=audit');
    await screen.findByRole('table', { name: 'Audit log' });
    const table = () => screen.getByRole('table', { name: 'Audit log' });
    const count = () => within(table()).getAllByRole('row').length - 1;

    await user.selectOptions(screen.getByLabelText('Actor'), 'usr_marco');
    expect(count()).toBe(5);
    await user.selectOptions(screen.getByLabelText('Action'), 'apikey.create');
    expect(count()).toBe(2);
    await user.type(screen.getByLabelText('Contains'), 'herald');
    expect(count()).toBe(1);
    expect(within(table()).getByText('key_02')).toBeInTheDocument();

    await user.type(screen.getByLabelText('Contains'), '-nothing');
    expect(screen.getByText('No loaded entry matches the filters.')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(count()).toBe(9);
  });

  it('loads older entries a page at a time', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=audit', testConfig({ pageSize: 4 }));
    const table = await screen.findByRole('table', { name: 'Audit log' });
    expect(within(table).getAllByRole('row')).toHaveLength(5);
    expect(screen.getByText(/the 4 entries loaded so far/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Load older entries' }));
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(9);
    });
    await user.click(screen.getByRole('button', { name: 'Load older entries' }));
    await waitFor(() => {
      expect(within(table).getAllByRole('row')).toHaveLength(10);
    });
    expect(screen.queryByRole('button', { name: 'Load older entries' })).not.toBeInTheDocument();
  });

  it('shows the actions of this session, such as a revoked key', async () => {
    const user = userEvent.setup();
    renderApp('/supply/settings?section=keys');
    await user.click(await screen.findByRole('button', { name: 'Revoke lbk_hrl7' }));
    await user.click(await screen.findByRole('button', { name: 'Revoke key' }));
    await screen.findByText('API key lbk_hrl7 revoked.');
    await user.click(screen.getByRole('tab', { name: 'Audit log' }));
    const table = await screen.findByRole('table', { name: 'Audit log' });
    const first = within(table).getAllByRole('row')[1];
    expect(within(first as HTMLElement).getByText('apikey.revoke')).toBeInTheDocument();
    expect(within(first as HTMLElement).getByText('key_02')).toBeInTheDocument();
  });
});

describe('audit filter', () => {
  const entry = (
    seq: number,
    actor: string,
    action: string,
    target: string | null,
  ): AuditEntry => ({
    seq,
    at: '2026-10-01T00:00:00Z',
    domain_id: 'supply',
    actor,
    action,
    target,
    details: { note: `Entry ${seq.toString()}` },
    entry_hash: '',
  });
  const entries = [
    entry(3, 'usr_b', 'branch.delete', 'old/x'),
    entry(2, 'usr_a', 'apikey.create', 'key_9'),
    entry(1, 'usr_a', 'branch.delete', null),
  ];

  it('combines the three filters', () => {
    expect(filterAudit(entries, NO_FILTER)).toHaveLength(3);
    expect(filterAudit(entries, { ...NO_FILTER, actor: 'usr_a' }).map((e) => e.seq)).toEqual([
      2, 1,
    ]);
    expect(
      filterAudit(entries, { actor: 'usr_a', action: 'branch.delete', text: '' }).map((e) => e.seq),
    ).toEqual([1]);
    expect(filterAudit(entries, { ...NO_FILTER, text: ' KEY_9 ' }).map((e) => e.seq)).toEqual([2]);
    expect(filterAudit(entries, { ...NO_FILTER, text: 'entry 3' }).map((e) => e.seq)).toEqual([3]);
  });

  it('lists the distinct values for the menus, sorted', () => {
    expect(distinct(entries, 'actor')).toEqual(['usr_a', 'usr_b']);
    expect(distinct(entries, 'action')).toEqual(['apikey.create', 'branch.delete']);
  });
});
