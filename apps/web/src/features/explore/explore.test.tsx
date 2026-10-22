import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMockApi } from '@/api/mock/adapter';
import { testConfig, type AppConfig } from '@/config/env';
import { urlOf } from '@/test/fetch';
import { renderApp } from '@/test/render';

const options = { pageSize: 50, searchLimit: 20, maxNodes: 200 };

const mockConfig = (): AppConfig => {
  const base = testConfig();
  return { ...base, explore: { ...base.explore, mockStepMs: 0 } };
};

/** Live mode against the mock adapter, with Guide behind the app's origin. */
const liveConfig = (guideUrl: string | null = '/guide'): AppConfig =>
  testConfig({ apiMode: 'live', keeperBaseUrl: '/', guideUrl, apiToken: 'tok-1' });

const sse = (name: string, data: unknown) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;

interface Call {
  url: string;
  init: RequestInit | undefined;
}

/** A Guide whose stream sends `chunks` and stays open unless `close`; aborting errors it. */
function stubGuide(chunks: string[], close = true) {
  const calls: Call[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: urlOf(input), init });
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          const encoder = new TextEncoder();
          for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
          if (close) controller.close();
          init?.signal?.addEventListener('abort', () => {
            controller.error(new DOMException('Aborted', 'AbortError'));
          });
        },
      });
      return Promise.resolve(
        new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }),
      );
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Explore', () => {
  it('plays a scripted exploration in mock mode, with links to the cited Records', async () => {
    const user = userEvent.setup();
    renderApp('/supply/explore', mockConfig());
    const presets = await screen.findByRole('group', { name: 'Preset questions' });
    await user.click(within(presets).getByRole('button', { name: 'Typo search "bering"' }));

    const answer = await screen.findByRole('region', { name: 'Answer' });
    expect(within(answer).getByText(/Bearings come from Brevik Bearings/)).toBeInTheDocument();
    expect(within(answer).getByRole('link', { name: 'Part/P-77812' })).toHaveAttribute(
      'href',
      '/supply/explorer/Part/P-77812',
    );
    expect(within(answer).getByRole('link', { name: 'View trace' })).toHaveAttribute(
      'href',
      '/supply/agent-activity?session=sess_20261020_brevik',
    );
    const steps = screen.getByRole('list', { name: 'Exploration steps' });
    expect(within(steps).getByText(/"q":"bering"/)).toBeInTheDocument();
    expect(within(steps).getByText('3 hits (lexical)')).toBeInTheDocument();
    expect(screen.getByText(/model scripted \(mock\)/)).toBeInTheDocument();
    expect(screen.getByText(/Done in 2 steps/)).toBeInTheDocument();
  });

  it('offers the preset questions on the supply Domain only', async () => {
    renderApp('/hr/explore', mockConfig());
    expect(await screen.findByLabelText('Question for the guide')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Preset questions' })).not.toBeInTheDocument();
  });

  it('renders streamed text as plain text and links only valid citations', async () => {
    const user = userEvent.setup();
    const calls = stubGuide([
      sse('session', { session_id: 'guide-abc', domain: 'supply', branch: null }),
      // An event split across two chunks.
      'event: thought\ndata: {"step": 0, "text": "<img src=x onerror=alert(1)>',
      ' [x](https://evil.test)"}\n\n',
      sse('tool_call', { step: 1, tool: 'search', args: { q: 'bearing' } }),
      sse('tool_result', {
        step: 1,
        tool: 'search',
        ok: true,
        summary: '<b>2</b> hits',
        refs: ['Part/P-77812'],
      }),
      sse('answer', {
        text: 'See [this](https://evil.test) ![i](https://evil.test/i.png) <img src=y>',
        found: true,
        citations: [
          { ref: 'Part/P-77812', kind: 'Part', key: 'P-77812' },
          { ref: 'Ghost/G-1', kind: 'Ghost', key: 'G-1' },
          { ref: 'https://evil.test', kind: 'https:', key: '//evil.test' },
        ],
      }),
      sse('done', {
        session_id: 'guide-abc',
        steps: 1,
        outcome: 'answered',
        tokens: { input: 10, output: 5 },
        elapsed_ms: 1200,
      }),
    ]);
    const { container } = renderApp('/supply/explore', liveConfig(), createMockApi(options));
    await user.type(await screen.findByLabelText('Question for the guide'), 'Who sells bearings?');
    await user.click(screen.getByRole('button', { name: 'Explore' }));

    const answer = await screen.findByRole('region', { name: 'Answer' });
    expect(
      within(answer).getByText(
        'See [this](https://evil.test) ![i](https://evil.test/i.png) <img src=y>',
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText('<img src=x onerror=alert(1)> [x](https://evil.test)'),
    ).toBeInTheDocument();
    expect(screen.getByText('<b>2</b> hits')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('a[href*="evil"]')).toBeNull();
    expect(within(answer).getByRole('link', { name: 'Part/P-77812' })).toBeInTheDocument();
    expect(within(answer).queryByRole('link', { name: 'Ghost/G-1' })).not.toBeInTheDocument();
    expect(within(answer).getByText('Ghost/G-1')).toBeInTheDocument();
    expect(within(answer).getByRole('link', { name: 'View trace' })).toHaveAttribute(
      'href',
      '/supply/agent-activity?session=guide-abc',
    );

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe('/guide/explore');
    expect(calls[0]?.init?.headers).toMatchObject({ Authorization: 'Bearer tok-1' });
    expect(JSON.parse(calls[0]?.init?.body as string)).toEqual({
      domain: 'supply',
      branch: null,
      question: 'Who sells bearings?',
      max_steps: 6,
    });
  });

  it('stops a running exploration and aborts the request', async () => {
    const user = userEvent.setup();
    const calls = stubGuide(
      [
        sse('session', { session_id: 'guide-abc', domain: 'supply', branch: null }),
        sse('thought', { step: 0, text: 'Thinking' }),
      ],
      false,
    );
    renderApp('/supply/explore', liveConfig(), createMockApi(options));
    await user.type(await screen.findByLabelText('Question for the guide'), 'q');
    await user.click(screen.getByRole('button', { name: 'Explore' }));
    expect(await screen.findByText('Thinking')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Stop' }));
    expect(await screen.findByText(/^Stopped\./)).toBeInTheDocument();
    expect(calls[0]?.init?.signal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Explore' })).toBeEnabled();
  });

  it('waits for Retry-After after a 429', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ code: 'rate' }), {
            status: 429,
            headers: { 'Content-Type': 'application/problem+json', 'Retry-After': '30' },
          }),
        ),
      ),
    );
    renderApp('/supply/explore', liveConfig(), createMockApi(options));
    await user.type(await screen.findByLabelText('Question for the guide'), 'q');
    await user.click(screen.getByRole('button', { name: 'Explore' }));
    expect(
      await screen.findByText(/too often in the last minute\. Try again in (29|30) s\./),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Explore' })).toBeDisabled();
    for (const preset of within(
      screen.getByRole('group', { name: 'Preset questions' }),
    ).getAllByRole('button')) {
      expect(preset).toBeDisabled();
    }
  });

  it('is absent in live mode without a guide service', async () => {
    renderApp('/supply/retrieval-lab', liveConfig(null), createMockApi(options));
    expect(
      await screen.findByRole('heading', { name: 'Retrieval lab', level: 1 }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Question for the guide')).not.toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).queryByRole('link', { name: 'Explore' })).not.toBeInTheDocument();
  });
});
