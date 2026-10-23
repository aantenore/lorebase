import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { messages } from '@/messages';
import { renderApp } from '@/test/render';

const SESSION = 'sess_20261020_brevik';

describe('Agent activity', () => {
  it('lists the sessions with their actor, calls, notes, tools and Proposals', async () => {
    renderApp('/supply/agent-activity');
    expect(
      await screen.findByRole('heading', { name: 'Agent activity', level: 1 }),
    ).toBeInTheDocument();

    const table = await screen.findByRole('table', { name: 'Agent sessions' });
    const row = within(table).getByRole('link', { name: SESSION }).closest('tr');
    if (row === null) throw new Error('no row');
    const cells = within(row);
    expect(cells.getByText('svc_herald')).toBeInTheDocument();
    expect(cells.getByText('search')).toBeInTheDocument();
    expect(cells.getByRole('link', { name: 'Proposal #41' })).toHaveAttribute(
      'href',
      expect.stringContaining('/supply/review/41'),
    );
    expect(within(table).getAllByText('inferred')).toHaveLength(1);
    expect(screen.getByText('Pick a session to see its timeline.')).toBeInTheDocument();
  });

  it('says so when tracing is off', async () => {
    const api = createMockApi({ pageSize: 50, searchLimit: 20, maxNodes: 200 });
    renderApp('/supply/agent-activity', testConfig(), {
      ...api,
      trace: {
        ...api.trace,
        sessions: () => Promise.resolve({ enabled: false, items: [], next_cursor: null }),
      },
    });
    expect(await screen.findByText(messages.agentActivity.tracingOff)).toBeInTheDocument();
    expect(await screen.findByText('No agent sessions match.')).toBeInTheDocument();
  });

  it('shows the timeline of a session from its URL', async () => {
    renderApp(`/supply/agent-activity?session=${SESSION}`);
    const timeline = await screen.findByRole('list', { name: 'Session timeline' });
    const steps = within(timeline).getAllByRole('listitem');
    expect(steps).toHaveLength(7);

    const search = steps[0] as HTMLElement;
    expect(within(search).getByText('POST /api/v1/domains/{domain_id}/search')).toBeInTheDocument();
    expect(within(search).getByText('200')).toBeInTheDocument();
    expect(within(search).getByText('21 ms')).toBeInTheDocument();
    expect(within(search).getByText('cache miss')).toBeInTheDocument();
    expect(within(search).getByText(/items 3/)).toBeInTheDocument();
    expect(within(search).getByText('beerings -> bearings')).toBeInTheDocument();
    expect(within(search).getByText(/"redacted"|"q": "brevik beerings"/)).toBeInTheDocument();
    expect(within(search).getByText(/Bitmap Heap Scan/)).toBeInTheDocument();

    const traverse = steps[2] as HTMLElement;
    const query = within(traverse).getByText(/"single_sourced"/).textContent ?? '';
    expect(query).toContain('"value": true');
    expect(query).toMatch(/"contact_email",\s+"op": "eq",\s+"value": \{\s+"redacted": "string"/);

    const context = steps[3] as HTMLElement;
    expect(within(context).getByText('Budget 1042 / 1200 tokens')).toBeInTheDocument();

    const proposal = steps[6] as HTMLElement;
    expect(within(proposal).getByRole('link', { name: 'Proposal #41' })).toBeInTheDocument();
  });

  it('shows the agent step of a note and flags an inferred session', async () => {
    renderApp(`/supply/agent-activity?session=${SESSION}`);
    expect(await screen.findByText('agent step 1')).toBeInTheDocument();
    expect(screen.getByText('agent step 3')).toBeInTheDocument();
    expect(screen.queryByText(/Grouped heuristically/)).not.toBeInTheDocument();
  });

  it('explains an inferred session in its timeline', async () => {
    renderApp('/supply/agent-activity?session=inferred-3f9a1c07d2b84e65');
    expect(
      await screen.findByText(/Grouped heuristically: these calls carried no session id/),
    ).toBeInTheDocument();
  });

  it('renders a note as plain text, never as markup', async () => {
    const { container } = renderApp(`/supply/agent-activity?session=${SESSION}`);
    const note = await screen.findByText(/Open a Proposal that flags the Supplier/);
    expect(note.textContent).toContain('<b>Not</b> merging anything myself.');
    expect(note.querySelector('b')).toBeNull();
    expect(container.querySelector('li b')).toBeNull();
  });

  it('draws the Records touched up to the step the slider picks', async () => {
    renderApp(`/supply/agent-activity?session=${SESSION}`);
    expect(
      await screen.findByRole('img', { name: 'Records touched: 6 nodes, 5 edges' }),
    ).toBeInTheDocument();

    const slider = screen.getByRole('slider', { name: 'Step' });
    expect(slider).toHaveValue('7');
    fireEvent.change(slider, { target: { value: '3' } });
    expect(
      await screen.findByRole('img', { name: 'Records touched: 4 nodes, 3 edges' }),
    ).toBeInTheDocument();
    expect(screen.getByText(/4 by step 3/)).toBeInTheDocument();
  });

  it('selects a step from the timeline and keeps it in the URL', async () => {
    const user = userEvent.setup();
    const { router } = renderApp(`/supply/agent-activity?session=${SESSION}`);
    await user.click(await screen.findByRole('link', { name: 'Show step 1 on the Canvas' }));
    await waitFor(() => {
      expect(router.state.location.search).toMatchObject({ session: SESSION, step: 1 });
    });
    expect(
      await screen.findByRole('img', { name: 'Records touched: 3 nodes, 0 edges' }),
    ).toBeInTheDocument();
  });
});
