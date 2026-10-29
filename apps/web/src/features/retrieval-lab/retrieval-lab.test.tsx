import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMockApi } from '@/api/mock/adapter';
import { testConfig } from '@/config/env';
import { messages } from '@/messages';
import { renderApp } from '@/test/render';
import { urlOf } from '@/test/fetch';

const T = messages.retrievalLab;

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

/** The hits of set A, which load after the screen. */
const resultsA = async () => within(await screen.findByRole('region', { name: 'Results A' }));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Retrieval lab', () => {
  it('asks for a question first, with no Explore panel and no link without an assistant', async () => {
    renderApp('/supply/retrieval-lab');
    expect(await screen.findByRole('heading', { name: T.title, level: 1 })).toBeInTheDocument();
    expect(screen.getByText(/Searching by words only/)).toBeInTheDocument();
    expect(screen.getByLabelText(T.vector.pasteLabel)).toBeInTheDocument();
    expect(screen.queryByLabelText('Question for the guide')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: T.askGuide })).not.toBeInTheDocument();
    expect(await screen.findByText(T.pack.askFirst)).toBeInTheDocument();
  });

  it('fills the Question from the example questions of the Domain', async () => {
    const user = userEvent.setup();
    renderApp('/supply/retrieval-lab');
    const examples = await screen.findByRole('group', { name: T.question.examples });
    await user.click(within(examples).getByRole('button', { name: 'Single-source risk' }));
    expect(screen.getByLabelText(T.question.label)).toHaveValue(
      'Which parts depend on a single supplier, and how risky is that supplier?',
    );
    expect(screen.getByLabelText(T.question.label)).toHaveAttribute(
      'placeholder',
      T.question.placeholder,
    );
  });

  it('offers no example questions for a Domain that declares none', async () => {
    renderApp('/hr/retrieval-lab');
    await screen.findByLabelText(T.question.label);
    expect(screen.queryByRole('group', { name: T.question.examples })).not.toBeInTheDocument();
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

  it('limits both searches and the pack to the Kinds of a lens', async () => {
    const user = userEvent.setup();
    renderApp('/supply/retrieval-lab?q=brevik');
    const a = await resultsA();
    expect(await a.findByText('Brevik master supply agreement')).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText(T.lens.label), 'Sites');
    expect(await a.findByText('Brevik Bearings S.p.A.')).toBeInTheDocument();
    expect(a.queryByText('Brevik master supply agreement')).not.toBeInTheDocument();
    const pack = await screen.findByRole('table', { name: 'Records in the context pack' });
    expect(within(pack).queryByText('Brevik master supply agreement')).not.toBeInTheDocument();
  });

  it('offers no lens when the schema declares none', async () => {
    renderApp('/hr/retrieval-lab');
    await screen.findByLabelText(T.question.label);
    expect(screen.queryByLabelText(T.lens.label)).not.toBeInTheDocument();
  });

  it('puts the question in the URL when asked', async () => {
    const user = userEvent.setup();
    const { router } = renderApp('/supply/retrieval-lab');
    await user.type(await screen.findByLabelText(T.question.label), 'bearing');
    await user.click(screen.getByRole('button', { name: T.question.ask }));
    expect(await (await resultsA()).findByText('Deep groove bearing 6204')).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ q: 'bearing' });
  });

  it('compares the two sets once a pasted vector lets B search by meaning', async () => {
    const user = userEvent.setup();
    renderApp('/supply/retrieval-lab?q=bearing');
    await (await resultsA()).findByText('Deep groove bearing 6204');
    await user.click(screen.getByLabelText(T.vector.pasteLabel));
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

  it('asks the assistant to embed the question and links to Explore', async () => {
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
    expect(await screen.findByText(/Embedded by mini: 4 dimensions/)).toBeInTheDocument();
    expect(calls).toEqual([
      { url: 'http://guide.test/embed', body: { text: 'bearing', mode: 'query' } },
    ]);
    expect(screen.getByRole('link', { name: T.askGuide })).toHaveAttribute(
      'href',
      expect.stringContaining('/supply/explore'),
    );
    expect(screen.queryByLabelText('Question for the guide')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(T.vector.pasteLabel)).not.toBeInTheDocument();
  });

  it('falls back to a pasted vector when the assistant fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(new Response('no', { status: 502 }))),
    );
    renderApp('/supply/retrieval-lab?q=bearing', testConfig({ guideUrl: 'http://guide.test' }));
    expect(await screen.findByText(/answered 502 to \/embed/)).toBeInTheDocument();
    expect(screen.getByLabelText(T.vector.pasteLabel)).toBeInTheDocument();
  });
});
