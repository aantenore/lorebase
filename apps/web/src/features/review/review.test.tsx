import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
    const conflicts = await screen.findByRole('table', { name: 'Conflicts' });
    expect(within(conflicts).getByText('DV1')).toBeInTheDocument();
    expect(within(conflicts).getByText('Unresolved')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Merge' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '1 Conflict still needs a resolution.',
    );
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Merge' })).toBeEnabled();
    });
  });
});
