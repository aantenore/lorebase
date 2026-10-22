import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { renderApp } from '@/test/render';
import { urlOf } from '@/test/fetch';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

/** The hits of set A, which load after the screen. */
const resultsA = async () => within(await screen.findByRole('region', { name: 'Results A' }));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Retrieval lab', () => {
  it('asks for a question first and has no Explore slot without a guide service', async () => {
    renderApp('/supply/retrieval-lab');
    expect(
      await screen.findByRole('heading', { name: 'Retrieval lab', level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/No guide service is configured/)).toBeInTheDocument();
    expect(screen.getByLabelText('Query vector (JSON array)')).toBeInTheDocument();
    expect(screen.queryByText('Explore (coming soon)')).not.toBeInTheDocument();
    expect(await screen.findByText('Ask a question to see its pack.')).toBeInTheDocument();
  });

  it('shows the hits of both sets, the corrections and the pack with the reasons', async () => {
    renderApp('/supply/retrieval-lab?q=brevik%20beerings&budget=900');
    const a = await screen.findByRole('region', { name: 'Results A' });
    expect(await within(a).findByText('Brevik Bearings S.p.A.')).toBeInTheDocument();
    expect(within(a).getByText('beerings -> bearings, bearing')).toBeInTheDocument();
    expect(await screen.findByRole('region', { name: 'Results B' })).toBeInTheDocument();

    const pack = await screen.findByRole('table', { name: 'Records in the context pack' });
    expect(
      within(pack).getAllByText(/Matched the search "brevik beerings"/).length,
    ).toBeGreaterThan(0);
    expect(await screen.findByText(/of 900 tokens/)).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Tokens per section' })).toBeInTheDocument();
    expect(
      await screen.findByRole('img', { name: /Involved Records: \d+ nodes/ }),
    ).toBeInTheDocument();
  });

  it('puts the question in the URL when asked', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/retrieval-lab');
    await user.type(await screen.findByLabelText('Question'), 'bearing');
    await user.click(screen.getByRole('button', { name: 'Ask' }));
    expect(await (await resultsA()).findByText('Deep groove bearing 6204')).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ q: 'bearing' });
  });

  it('compares the two sets once a pasted vector lets B search by meaning', async () => {
    const user = userEvent.setup();
    renderApp('/supply/retrieval-lab?q=bearing');
    await (await resultsA()).findByText('Deep groove bearing 6204');
    await user.click(screen.getByLabelText('Query vector (JSON array)'));
    await user.paste('[0.2, 0.7, 0.1]');
    expect(await screen.findByText('3 dimensions')).toBeInTheDocument();
    expect(await screen.findByText(/in both, \d+ only in A, \d+ only in B/)).toBeInTheDocument();
  });

  it('renders snippets as text, whatever markup they hold', async () => {
    const api = createMockApi(options);
    const { container } = renderApp('/supply/retrieval-lab?q=bearing', testConfig(), {
      ...api,
      search: {
        run: (scope, request) =>
          api.search.run(scope, request).then((response) => ({
            ...response,
            results: response.results.map((hit) => ({
              ...hit,
              snippet: '<img src=x onerror=alert(1)> <b>bearing</b>',
            })),
          })),
      },
    });
    const snippets = await screen.findAllByText(/<img src=x onerror=alert\(1\)> <b>bearing<\/b>/);
    expect(snippets.length).toBeGreaterThan(0);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('li b')).toBeNull();
  });

  it('asks the guide service to embed the question and shows its slot', async () => {
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: urlOf(input),
          body: JSON.parse(typeof init?.body === 'string' ? init.body : 'null'),
        });
        return Promise.resolve(
          new Response(JSON.stringify({ vector: [0.1, 0.2, 0.3, 0.4], model: 'mini', dims: 4 }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }),
    );
    renderApp('/supply/retrieval-lab?q=bearing', testConfig({ guideUrl: 'http://guide.test' }));
    expect(
      await screen.findByText(/Embedded by the guide service \(mini\): 4 dimensions/),
    ).toBeInTheDocument();
    expect(calls).toEqual([
      { url: 'http://guide.test/embed', body: { text: 'bearing', mode: 'query' } },
    ]);
    expect(screen.getByText('Explore (coming soon)')).toBeInTheDocument();
    expect(screen.queryByLabelText('Query vector (JSON array)')).not.toBeInTheDocument();
  });

  it('falls back to a pasted vector when the guide service fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('no', { status: 502 }))),
    );
    renderApp('/supply/retrieval-lab?q=bearing', testConfig({ guideUrl: 'http://guide.test' }));
    expect(await screen.findByText(/answered 502 to \/embed/)).toBeInTheDocument();
    expect(screen.getByLabelText('Query vector (JSON array)')).toBeInTheDocument();
  });
});
