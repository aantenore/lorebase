import { compactArgs, parseExploreEvent, safeCitation } from './events';
import { GuideError, liveTransport, parseRetryAfter } from './explore-client';
import { scriptedEvents } from './mock-guide';

describe('citations', () => {
  it('accepts a Kind name, a plain key and a matching ref', () => {
    expect(safeCitation({ ref: 'Part/P-1', kind: 'Part', key: 'P-1' })).toEqual({
      ref: 'Part/P-1',
      kind: 'Part',
      key: 'P-1',
    });
    expect(safeCitation({ kind: 'Doc', key: 'a/b c' })).toEqual({
      ref: 'Doc/a/b c',
      kind: 'Doc',
      key: 'a/b c',
    });
  });

  it.each([
    [{ ref: 'Part/P-2', kind: 'Part', key: 'P-1' }],
    [{ kind: 'part', key: 'P-1' }],
    [{ kind: 'javascript:alert(1)//', key: 'x' }],
    [{ kind: 'Part', key: '..' }],
    [{ kind: 'Part', key: '' }],
    [{ kind: 'Part', key: 'a\nb' }],
    [{ kind: 'Part', key: 'x'.repeat(513) }],
    [{ kind: 'Part', key: 7 }],
    ['Part/P-1'],
    [null],
  ])('refuses %j', (raw) => {
    expect(safeCitation(raw)).toBeNull();
  });
});

describe('explore events', () => {
  it('reads the documented events and drops unknown or malformed ones', () => {
    expect(
      parseExploreEvent({ event: 'session', data: '{"session_id":"guide-1","domain":"d"}' }),
    ).toEqual({ type: 'session', session_id: 'guide-1', domain: 'd', branch: null });
    expect(
      parseExploreEvent({
        event: 'answer',
        data: JSON.stringify({
          text: 'Yes',
          found: true,
          citations: [
            { ref: 'Part/P-1', kind: 'Part', key: 'P-1' },
            { ref: 'https://evil.test', kind: 'https:', key: '//evil.test' },
          ],
        }),
      }),
    ).toEqual({
      type: 'answer',
      text: 'Yes',
      found: true,
      citations: [{ ref: 'Part/P-1', kind: 'Part', key: 'P-1' }],
    });
    expect(parseExploreEvent({ event: 'unknown', data: '{}' })).toBeNull();
    expect(parseExploreEvent({ event: 'thought', data: 'not json' })).toBeNull();
    expect(compactArgs({ q: 'x'.repeat(300) }, 20)).toHaveLength(20);
  });

  it('cuts a script to max_steps tool calls, as Guide does', () => {
    const events = scriptedEvents({
      domain: 'supply',
      branch: null,
      question: 'Who supplies bering parts?',
      max_steps: 1,
    });
    expect(events.filter((e) => e.type === 'tool_call')).toHaveLength(1);
    expect(events.at(-1)).toMatchObject({ type: 'done', steps: 1 });
  });
});

describe('the live transport', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads Retry-After as seconds or a date', () => {
    expect(parseRetryAfter('7')).toBe(7);
    expect(parseRetryAfter(new Date(10_500).toUTCString(), 0)).toBe(10);
    expect(parseRetryAfter('soon')).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });

  it('sends the session cookie and the CSRF header to an absolute guide URL', async () => {
    const fetchMock = vi.fn((_url: string, _init: RequestInit) =>
      Promise.resolve(
        new Response('event: thought\ndata: {"step":0,"text":"hi"}\n\n', {
          headers: { 'Content-Type': 'text/event-stream' },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const explore = liveTransport('https://app.test/guide', () => ({
      headers: { 'X-CSRF-Token': 'c1' },
      credentials: 'include',
    }));
    const events = [];
    for await (const event of explore(
      { domain: 'supply', branch: null, question: 'q' },
      new AbortController().signal,
    )) {
      events.push(event);
    }
    expect(events).toEqual([{ type: 'thought', step: 0, text: 'hi' }]);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe('https://app.test/guide/explore');
    expect(init?.credentials).toBe('include');
    expect(init?.headers).toMatchObject({ 'X-CSRF-Token': 'c1', Accept: 'text/event-stream' });
  });

  it('turns a problem document into a GuideError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ code: 'csrf' }), {
            status: 403,
            headers: { 'Content-Type': 'application/problem+json' },
          }),
        ),
      ),
    );
    const explore = liveTransport('/guide', () => ({ headers: {}, credentials: 'same-origin' }));
    const run = explore({ domain: 'd', branch: null, question: 'q' }, new AbortController().signal);
    const error: unknown = await run.next().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(GuideError);
    expect(error).toMatchObject({ status: 403, code: 'csrf', retryAfterS: null });
  });
});
