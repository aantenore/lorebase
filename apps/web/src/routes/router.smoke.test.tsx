import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

/** Bounded wait for a screen to settle on a loaded machine (the default is one second). */
const SLOW_RENDER_MS = 5000;

const heading = (name: string) => screen.findByRole('heading', { name, level: 1 });

describe('router smoke', () => {
  it('opens the first Domain Explorer from the root', async () => {
    const { router } = renderApp('/');
    expect(await heading('Explorer')).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/supply/explorer');
  });

  it('serves every area from the left navigation', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer');
    await heading('Explorer');

    const nav = screen.getByRole('navigation', { name: 'Main' });
    for (const area of ['Canvas', 'Modeler', 'Review', 'Settings']) {
      await user.click(within(nav).getByRole('link', { name: area }));
      expect(await heading(area)).toBeInTheDocument();
      expect(router.state.location.pathname).toBe(`/supply/${area.toLowerCase()}`);
    }
  });

  it('keeps the Branch in the URL while navigating', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/explorer?branch=model%2Frisk');
    await heading('Explorer');
    await user.click(
      within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', {
        name: 'Modeler',
      }),
    );
    await heading('Modeler');
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ branch: 'model/risk' });
    });
  });

  it('shows the not found view for unknown paths', async () => {
    renderApp('/a/b/c/d/e/nowhere');
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });

  it('reports an unknown Branch instead of rendering the screen', async () => {
    renderApp('/supply/explorer?branch=nope');
    // The failed query is reported twice: by the screen (a paragraph) and, later, by a toast.
    // Only the paragraph is awaited, so the assertion does not depend on the toast's timing.
    const report = await screen.findByText(
      /Branch nope does not exist/,
      { selector: 'p' },
      { timeout: SLOW_RENDER_MS },
    );
    expect(report).toHaveAttribute('role', 'alert');
    expect(screen.queryByRole('heading', { name: 'Explorer', level: 1 })).not.toBeInTheDocument();
  });
});
