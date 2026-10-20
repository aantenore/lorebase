import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

describe('app shell', () => {
  it('shows the Domain, Branch, head Revision and distance from the default Branch', async () => {
    renderApp('/supply/modeler?branch=model%2Frisk');
    const branch = await screen.findByLabelText('Branch');
    expect(branch).toHaveValue('model/risk');
    expect(screen.getByLabelText('Domain')).toHaveValue('supply');
    expect(await screen.findByText(/Revision f2a8c3d5/)).toBeInTheDocument();
    expect(screen.getByText('behind main by 1')).toBeInTheDocument();
  });

  it('switches Branch and every view follows', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/modeler');
    expect(await screen.findByRole('cell', { name: /^Supplier/ })).toBeInTheDocument();
    expect(screen.queryByText('RiskFlag')).not.toBeInTheDocument();

    await user.selectOptions(await screen.findByLabelText('Branch'), 'model/risk');

    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ branch: 'model/risk' });
    });
    expect(await screen.findByText('RiskFlag')).toBeInTheDocument();
  });

  it('switches Domain and drops the Branch', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer?branch=model%2Frisk');
    const domainSelect = await screen.findByLabelText('Domain');
    await screen.findByRole('option', { name: 'Human resources' });
    await user.selectOptions(domainSelect, 'hr');
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/hr/explorer');
    });
    expect(router.state.location.search).not.toHaveProperty('branch');
  });

  it('opens global search with the keyboard and lands on Explorer results', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/canvas');
    await screen.findByRole('heading', { name: 'Canvas', level: 1 });

    await user.keyboard('{Control>}k{/Control}');
    const dialog = await screen.findByRole('dialog', { name: 'Search Records' });
    expect(dialog).toBeInTheDocument();
    await user.type(screen.getByLabelText('Search query'), 'Brevik{Enter}');

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/supply/explorer');
    });
    expect(router.state.location.search).toMatchObject({ q: 'Brevik' });
  });
});
