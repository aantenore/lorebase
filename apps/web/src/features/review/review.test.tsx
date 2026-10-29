import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { messages } from '@/messages';
import type { KeeperApi } from '@/api';
import { createMockApi, asRemote } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';

describe('Review', () => {
  it('lists open Proposals with their Conflict counts', async () => {
    renderApp('/supply/review');
    expect(await screen.findByRole('heading', { name: 'Review', level: 1 })).toBeInTheDocument();
    const table = await screen.findByRole('table', { name: 'Proposals' });

    const risk = within(table).getByRole('row', { name: /Risk model/ });
    expect(await within(risk).findByText('6 (6 unresolved)')).toBeInTheDocument();
    const contracts = within(table).getByRole('row', { name: /Contracts batch October/ });
    expect(await within(contracts).findByText('0')).toBeInTheDocument();
    expect(within(table).queryByText('Sync erp.vendors run 6')).not.toBeInTheDocument();
  });

  it('filters by status', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review');
    await user.selectOptions(await screen.findByLabelText('Status'), 'merged');
    expect(await screen.findByText('Sync erp.vendors run 6')).toBeInTheDocument();
    expect(screen.queryByText('Contracts batch October')).not.toBeInTheDocument();
  });

  it('does not merge while Conflicts are unresolved', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review/43');
    expect(await screen.findByRole('heading', { name: '#43 Tier cleanup' })).toBeInTheDocument();
    const conflicts = await screen.findByRole('listbox', { name: 'Conflicts' });
    expect(within(conflicts).getByText('open')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Merge' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '1 Conflict still needs a resolution.',
    );
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Merge' })).toBeEnabled();
    });
  });
});

describe('Review form', () => {
  it('adds a review and shows it with the approval state', async () => {
    const user = userEvent.setup();
    renderApp('/supply/review/42');
    expect(
      await screen.findByRole('heading', { name: /Contracts batch October/ }),
    ).toBeInTheDocument();
    expect(screen.getByText('No reviews yet.')).toBeInTheDocument();
    expect(screen.getByText('The target Branch needs no approval.')).toBeInTheDocument();

    const form = screen.getByRole('form', { name: 'Add a review' });
    await user.selectOptions(within(form).getByLabelText('Verdict'), 'request_changes');
    await user.type(within(form).getByLabelText('Comment'), 'Quote the page, please.');
    await user.click(within(form).getByRole('button', { name: 'Submit review' }));

    expect(await screen.findByText('Review added.')).toBeInTheDocument();
    const review = await screen.findByText('Quote the page, please.');
    expect(review.closest('li')).toHaveTextContent('request_changes');
    expect(review.closest('li')).toHaveTextContent('usr_mock');
    expect(within(form).getByLabelText('Comment')).toHaveValue('');
  });
});

const options = () =>
  within(screen.getByRole('listbox', { name: 'Conflicts' })).getAllByRole('option');
const group = (code: string) => screen.getByRole('group', { name: new RegExp(`^${code} `) });
const optionIn = (code: string) => within(group(code)).getByRole('option');

async function openRiskProposal() {
  const user = userEvent.setup();
  const utils = renderApp('/supply/review/41');
  const list = await screen.findByRole('listbox', { name: 'Conflicts' });
  return { user, list, ...utils };
}

describe('Review merge attempt', () => {
  it('groups the Conflicts by kind with counts and shows the progress', async () => {
    await openRiskProposal();
    const codes = within(screen.getByRole('listbox', { name: 'Conflicts' }))
      .getAllByRole('group')
      .map((g) => g.getAttribute('aria-label'));
    expect(codes).toEqual([
      'DV1 Attribute value',
      'DV2 Delete or modify',
      'SC1 Schema property',
      'XS4 Missing value',
      'XS5 Constraint',
      'XS9 Dangling Link',
    ]);
    expect(screen.getByText('0 of 6 resolved')).toBeInTheDocument();
    expect(within(group('XS4')).getByText('3 members')).toBeInTheDocument();
    expect(within(optionIn('DV1')).getByText('open')).toBeInTheDocument();
    expect(screen.getByText('Resolved by policy (1)')).toBeInTheDocument();
  });

  it('shows element names instead of ids in the list and the resolver', async () => {
    await openRiskProposal();
    expect(
      within(optionIn('SC1')).getByText(/Supplier · attributes\.risk_score\.description/),
    ).toBeInTheDocument();
    expect(within(optionIn('DV1')).getByText('Supplier/S-1003 · tier')).toBeInTheDocument();
  });

  it('opens the first Conflict and moves between Conflicts with the keyboard', async () => {
    const { user, list, router } = await openRiskProposal();
    const selected = () =>
      options()
        .filter((o) => o.getAttribute('aria-selected') === 'true')
        .map((o) => o.textContent ?? '');

    expect(selected()).toEqual([expect.stringContaining('Supplier/S-1003 · tier')]);
    list.focus();
    await user.keyboard('{ArrowDown}');
    expect(selected()).toEqual([expect.stringContaining('Plant/MNZ')]);
    expect(
      await screen.findByRole('region', { name: /^Conflict DV2 Plant\/MNZ/ }),
    ).toBeInTheDocument();

    await user.keyboard('j');
    expect(selected()).toEqual([expect.stringContaining('attributes.risk_score.description')]);
    await user.keyboard('k');
    expect(selected()).toEqual([expect.stringContaining('Plant/MNZ')]);
    await user.keyboard('{End}');
    expect(selected()).toEqual([expect.stringContaining('feeds')]);
    await user.keyboard('{ArrowDown}');
    expect(selected()).toEqual([expect.stringContaining('Supplier/S-1003 · tier')]);
    await user.keyboard('{ArrowUp}');
    expect(selected()).toEqual([expect.stringContaining('feeds')]);
    await user.keyboard('{Home}');
    expect(selected()).toEqual([expect.stringContaining('Supplier/S-1003 · tier')]);

    await waitFor(() => {
      expect(router.state.location.search).toHaveProperty('conflict');
    });
  });

  it('opens the Conflict named in the URL', async () => {
    const { user, list, router, unmount } = await openRiskProposal();
    list.focus();
    await user.keyboard('{ArrowDown}');
    await waitFor(() => {
      expect(router.state.location.search).toHaveProperty('conflict');
    });
    const search: { conflict?: string } = router.state.location.search;
    unmount();

    renderApp(`/supply/review/41?conflict=${search.conflict ?? ''}`);
    expect(
      await screen.findByRole('region', { name: /^Conflict DV2 Plant\/MNZ/ }),
    ).toBeInTheDocument();
  });

  it('filters the list by kind, status and text', async () => {
    const { user } = await openRiskProposal();
    await user.selectOptions(screen.getByLabelText(messages.review.filters.type), 'XS5');
    expect(options()).toHaveLength(1);
    expect(screen.getByRole('region', { name: /^Conflict XS5/ })).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(messages.review.filters.type), 'all');
    await user.type(screen.getByLabelText('Find a Conflict'), 'S-1005');
    expect(options()).toHaveLength(1);
    expect(
      within(screen.getByRole('listbox', { name: 'Conflicts' })).getByText('3 members'),
    ).toBeInTheDocument();

    await user.clear(screen.getByLabelText('Find a Conflict'));
    await user.selectOptions(screen.getByLabelText('Status'), 'resolved');
    expect(screen.getByText('No Conflict matches the filter.')).toBeInTheDocument();
  });

  it('applies what is resolved and reports the Conflicts that remain', async () => {
    const { user } = await openRiskProposal();
    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(await screen.findByText('1 of 6 resolved')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Apply resolutions' }));
    const dialog = await screen.findByRole('dialog', { name: 'Apply with open Conflicts' });
    expect(dialog).toHaveTextContent('5 Conflicts are still open');
    await user.click(within(dialog).getByRole('button', { name: 'Apply anyway' }));
    expect(await screen.findByText(/Merge run: 5 Conflicts remain/)).toBeInTheDocument();
    expect(screen.getByText('1 of 6 resolved')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Merged Revision' })).not.toBeInTheDocument();
  });

  it('does not apply when the confirmation about open Conflicts is cancelled', async () => {
    const { user } = await openRiskProposal();
    await user.click(screen.getByRole('button', { name: 'Apply resolutions' }));
    const dialog = await screen.findByRole('dialog', { name: 'Apply with open Conflicts' });
    expect(dialog).toHaveTextContent('6 Conflicts are still open');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText(/Merge run:/)).not.toBeInTheDocument();
  });

  it('opens the next open Conflict after a save and names the Branches in the headers', async () => {
    const { user } = await openRiskProposal();
    expect(screen.getAllByRole('columnheader', { name: /^Ours \(.+\)$/ }).length).toBeGreaterThan(
      0,
    );

    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(await screen.findByText('1 of 6 resolved')).toBeInTheDocument();
    expect(
      await screen.findByRole('region', { name: /^Conflict DV2 Plant\/MNZ/ }),
    ).toBeInTheDocument();
  });

  it('drives the resolver from the keyboard', async () => {
    const { user } = await openRiskProposal();
    await user.keyboard('t');
    expect(screen.getByRole('radio', { name: 'Take theirs' })).toBeChecked();
    await user.keyboard('{Enter}');
    expect(await screen.findByText('1 of 6 resolved')).toBeInTheDocument();
    expect(
      await screen.findByRole('region', { name: /^Conflict DV2 Plant\/MNZ/ }),
    ).toBeInTheDocument();
    await user.keyboard('n');
    expect(
      await screen.findByRole('region', { name: /^Conflict SC1 Supplier/ }),
    ).toBeInTheDocument();
  });

  it('does not save on Enter pressed in the list', async () => {
    const { user, list } = await openRiskProposal();
    await user.keyboard('t');
    list.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByText('0 of 6 resolved')).toBeInTheDocument();
    await user.keyboard('j');
    expect(screen.queryByRole('dialog', { name: 'Unsaved resolution' })).toBeInTheDocument();
  });

  it('keeps the Conflict the person moved to while a save is in flight', async () => {
    const mock = asRemote(createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 }), 0);
    const api: KeeperApi = {
      ...mock,
      conflicts: {
        ...mock.conflicts,
        resolve: async (...args) => {
          await new Promise((done) => setTimeout(done, 150));
          return mock.conflicts.resolve(...args);
        },
      },
    };
    const user = userEvent.setup();
    renderApp('/supply/review/41', testConfig(), api);
    await screen.findByRole('listbox', { name: 'Conflicts' });

    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await user.click(optionIn('XS5'));
    await user.click(
      within(await screen.findByRole('dialog', { name: 'Unsaved resolution' })).getByRole(
        'button',
        {
          name: 'Discard and continue',
        },
      ),
    );
    expect(await screen.findByText('1 of 6 resolved')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: /^Conflict XS5/ })).toBeInTheDocument();
  });

  it('shows a bulk resolution in the open resolver without calling it unsaved', async () => {
    const { user } = await openRiskProposal();
    await user.selectOptions(screen.getByLabelText('Bulk choice'), 'ours');
    await user.click(screen.getByRole('button', { name: /^Apply "Take ours" to \d+ open$/ }));
    expect(await screen.findByText('resolved: ours')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Take ours' })).toBeChecked();
    expect(screen.getByRole('button', { name: 'Update resolution' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Apply resolutions' }));
    expect(await screen.findByRole('dialog')).not.toHaveTextContent('not saved');
  });

  it('bulk-resolves only the open Conflicts and counts what it sent', async () => {
    const { user } = await openRiskProposal();
    await user.click(screen.getByRole('radio', { name: 'Take theirs' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    expect(await screen.findByText('1 of 6 resolved')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Bulk choice'), 'ours');
    const button = screen.getByRole('button', { name: /^Apply "Take ours" to \d+ open$/ });
    const sent = /to (\d+) open/.exec(button.textContent)?.[1] ?? '';
    await user.click(button);
    expect(await screen.findByText(`${sent} resolutions stored.`)).toBeInTheDocument();
    expect(within(optionIn('DV1')).getByText('theirs')).toBeInTheDocument();
  });

  it('mentions unsaved changes when applying', async () => {
    const { user } = await openRiskProposal();
    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Apply resolutions' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('changes that are not saved');
  });

  it('keeps the filter when leaving an unsaved Conflict is cancelled', async () => {
    const { user } = await openRiskProposal();
    await user.selectOptions(screen.getByLabelText(messages.review.filters.type), 'DV1');
    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Next open Conflict' }));
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: 'Keep editing' }),
    );
    expect(screen.getByLabelText(messages.review.filters.type)).toHaveValue('DV1');
  });

  it('asks before leaving a Conflict with unsaved changes', async () => {
    const { user } = await openRiskProposal();
    await user.click(screen.getByRole('radio', { name: 'Take ours' }));
    await user.click(optionIn('DV2'));

    const dialog = await screen.findByRole('dialog', { name: 'Unsaved resolution' });
    await user.click(within(dialog).getByRole('button', { name: 'Keep editing' }));
    expect(screen.getByRole('radio', { name: 'Take ours' })).toBeChecked();

    await user.click(optionIn('DV2'));
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Discard and continue',
      }),
    );
    expect(
      await screen.findByRole('region', { name: /^Conflict DV2 Plant\/MNZ/ }),
    ).toBeInTheDocument();
  });

  it('labels the bulk choice like the resolver does', async () => {
    const { user } = await openRiskProposal();
    await user.selectOptions(screen.getByLabelText('Bulk choice'), 'restore');
    expect(
      screen.getByRole('button', { name: 'Apply "Restore the Record" to 1 open' }),
    ).toBeInTheDocument();
  });

  it('resolves every Conflict, re-runs the merge and summarizes the merged Revision', async () => {
    const { user } = await openRiskProposal();
    const saved = (n: number) => screen.findByText(`${n} of 6 resolved`);

    // DV1: pick a side.
    await user.click(screen.getByRole('radio', { name: 'Take theirs' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await saved(1);

    // SC1: a schema property.
    await user.click(optionIn('SC1'));
    await user.click(await screen.findByRole('radio', { name: 'Take ours' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await saved(2);

    // DV2: keep the modified Record.
    await user.click(optionIn('DV2'));
    await user.click(await screen.findByRole('radio', { name: 'Keep modified (ours)' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await saved(3);

    // XS9: a bulk choice that only the Conflicts accepting it receive.
    await user.selectOptions(screen.getByLabelText('Bulk choice'), 'restore');
    await user.click(screen.getByRole('button', { name: 'Apply "Restore the Record" to 1 open' }));
    await saved(4);

    // XS4: one value for the whole group.
    await user.click(optionIn('XS4'));
    await user.click(await screen.findByRole('radio', { name: 'Custom value' }));
    await user.selectOptions(screen.getByLabelText('Same value for every member'), 'low');
    await user.click(screen.getByRole('button', { name: 'Apply to all members' }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await saved(5);

    // XS5: relax the constraint.
    await user.click(optionIn('XS5'));
    await user.click(await screen.findByRole('radio', { name: /^Relax the rule/ }));
    await user.click(screen.getByRole('button', { name: 'Save resolution' }));
    await saved(6);

    await user.click(screen.getByRole('button', { name: 'Apply resolutions' }));
    const summary = await screen.findByRole('region', { name: 'Merged Revision' });
    const applied = within(summary).getByRole('table', { name: 'Resolutions applied' });
    expect(within(applied).getAllByRole('row')).toHaveLength(1 + 6 + 1);
    expect(within(summary).getByText('1 by policy')).toBeInTheDocument();
    expect(await within(summary).findByText(/\(target\)/)).toBeInTheDocument();
    expect(within(summary).getByText(/\(source\)/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Apply resolutions' })).not.toBeInTheDocument();
  });
});
