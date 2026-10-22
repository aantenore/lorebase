import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

async function openDerivations() {
  const user = userEvent.setup();
  renderApp('/supply/modeler?view=edit&branch=model%2Frisk');
  await screen.findByRole('textbox', { name: 'Schema YAML' });
  await user.click(screen.getByRole('tab', { name: /^Derivations/ }));
  return user;
}

describe('Derivation preview', () => {
  it('shows the computed Attribute on sample Records of the saved Derivation', async () => {
    await openDerivations();
    const table = await screen.findByRole('table', { name: /^Sample values of / });
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1);
    expect(within(table).getAllByRole('row').length).toBeLessThanOrEqual(6);
  });

  it('asks for the change set to be saved once the Derivation is edited', async () => {
    const user = await openDerivations();
    await screen.findByRole('table', { name: /^Sample values of / });
    const description = screen.getByRole('textbox', { name: 'Derivation description' });
    await user.type(description, ' edited');
    expect(await screen.findByText(/Save the change set first: the preview/)).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: /^Sample values of / })).toBeNull();
  });
});
