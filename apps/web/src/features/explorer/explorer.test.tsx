import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

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
    const box = await screen
      .findByRole('searchbox')
      .catch(() => screen.findByLabelText('Search Records'));
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
});
