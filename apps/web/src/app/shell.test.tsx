import { screen, waitFor, within } from '@testing-library/react';
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

  it('creates a Branch from the Branch on screen and switches to it', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer?branch=model%2Frisk');
    await screen.findByLabelText('Branch');
    await user.click(screen.getByRole('button', { name: 'New Branch' }));
    const dialog = await screen.findByRole('dialog', { name: 'New Branch' });
    expect(dialog).toHaveTextContent('starts at the head of model/risk');
    await user.type(within(dialog).getByLabelText('Branch name'), 'work/ui');
    await user.click(within(dialog).getByRole('button', { name: 'Create Branch' }));

    expect(await screen.findByText('Branch work/ui created from model/risk.')).toBeInTheDocument();
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ branch: 'work/ui' });
    });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(await screen.findByLabelText('Branch')).toHaveValue('work/ui');
  });

  it('shows a refused create as a toast and keeps the dialog open', async () => {
    const user = userEvent.setup();
    renderApp('/supply/explorer');
    await screen.findByLabelText('Branch');
    await user.click(screen.getByRole('button', { name: 'New Branch' }));
    const dialog = await screen.findByRole('dialog', { name: 'New Branch' });
    await user.type(within(dialog).getByLabelText('Branch name'), 'model/risk');
    await user.click(within(dialog).getByRole('button', { name: 'Create Branch' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Branch model/risk exists.');
    expect(screen.getByRole('dialog', { name: 'New Branch' })).toBeInTheDocument();
  });
});
