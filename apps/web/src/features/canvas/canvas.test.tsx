import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderApp } from '@/test/render';

describe('Canvas', () => {
  it('draws the sample Domain as a graph and lists it textually', async () => {
    renderApp('/supply/canvas');
    expect(await screen.findByRole('heading', { name: 'Canvas', level: 1 })).toBeInTheDocument();

    const graph = await screen.findByRole('img', { name: /Records graph: \d+ nodes, \d+ edges/ });
    expect(graph).toBeInTheDocument();
    const summary = await screen.findByText(/List view: \d+ nodes/);
    const nodes = Number(/(\d+) nodes/.exec(summary.textContent)?.[1]);
    expect(nodes).toBeGreaterThan(10);
    // No WebGL in the test environment: the canvas explains itself instead of failing.
    expect(screen.getByText(/Graph drawing is unavailable/)).toBeInTheDocument();
  });

  it('shows the schema graph with Relations and cardinality', async () => {
    const user = userEvent.setup();
    renderApp('/supply/canvas');
    await user.click(await screen.findByRole('tab', { name: 'Schema graph' }));
    expect(
      await screen.findByRole('img', { name: 'Schema graph: 4 nodes, 3 edges' }),
    ).toBeInTheDocument();
  });

  it('keeps the diff overlay as a planned view', async () => {
    const user = userEvent.setup();
    renderApp('/supply/canvas');
    await user.click(await screen.findByRole('tab', { name: 'Diff overlay' }));
    expect(await screen.findByText('Planned')).toBeInTheDocument();
  });
});
