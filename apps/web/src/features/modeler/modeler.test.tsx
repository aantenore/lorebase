import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

describe('Modeler', () => {
  it('lists Kinds, Attributes and Relations of the Branch', async () => {
    const user = userEvent.setup();
    renderApp('/supply/modeler');
    expect(await screen.findByRole('heading', { name: 'Modeler', level: 1 })).toBeInTheDocument();

    const kinds = await screen.findByRole('table', { name: 'Kinds' });
    expect(within(kinds).getByText('Supplier')).toBeInTheDocument();
    expect(within(kinds).getByText('Contract')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /^Attributes/ }));
    const attributes = await screen.findByRole('table', { name: 'Attributes' });
    expect(within(attributes).getByText('Supplier.risk_score')).toBeInTheDocument();
    expect(within(attributes).getByText('derived')).toBeInTheDocument();

    await user.click(screen.getByRole('tab', { name: /^Relations/ }));
    const relations = await screen.findByRole('table', { name: 'Relations' });
    expect(within(relations).getByText('supplied_by')).toBeInTheDocument();
    expect(within(relations).getAllByText('many_to_many').length).toBeGreaterThan(0);
  });

  it('explains that a protected Branch is edited through another Branch', async () => {
    renderApp('/supply/modeler');
    expect(await screen.findByText(/main is protected/)).toBeInTheDocument();
  });

  it('moves between tabs with the keyboard', async () => {
    const user = userEvent.setup();
    renderApp('/supply/modeler');
    const first = await screen.findByRole('tab', { name: /^Kinds/ });
    first.focus();
    await user.keyboard('{ArrowRight}');
    expect(screen.getByRole('tab', { name: /^Attributes/ })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });

  it('has no planned editors left in the screen inventory', async () => {
    renderApp('/supply/modeler');
    expect(await screen.findByRole('table', { name: 'Kinds' })).toBeInTheDocument();
    expect(screen.queryByText('Modeler: planned screens')).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Modeler: Operation editor' })).toBeNull();
  });
});
