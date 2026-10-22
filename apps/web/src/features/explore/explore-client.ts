import type { GuideAuth } from '@/features/retrieval-lab/guide-client';
import { parseExploreEvent, type ExploreEvent } from './events';
import { readSse } from './sse';

/** Body of `POST {guideUrl}/explore`; `max_steps` can only lower Guide's own budget. */
export interface ExploreRequest {
  domain: string;
  branch: string | null;
  question: string;
  max_steps?: number;
}

/** Streams the events of one exploration; ends when Guide closes the stream or `signal` aborts. */
export type ExploreTransport = (
  request: ExploreRequest,
  signal: AbortSignal,
) => AsyncGenerator<ExploreEvent>;

/** A refusal before the stream started (a problem document) or a failure to reach Guide. */
export class GuideError extends Error {
  constructor(
    message: string,
    readonly status: number | null,
    readonly code: string,
    /** Seconds to wait before asking again (429 with `Retry-After`). */
    readonly retryAfterS: number | null = null,
  ) {
    super(message);
    this.name = 'GuideError';
  }
}

const MAX_RETRY_S = 3600;

/** `Retry-After` as seconds from now: delta-seconds or an HTTP date; `null` when absent or bad. */
export function parseRetryAfter(value: string | null, now = Date.now()): number | null {
  if (value === null || value.trim() === '') return null;
  const text = value.trim();
  const seconds = /^\d+$/.test(text) ? Number(text) : (Date.parse(text) - now) / 1000;
  if (!Number.isFinite(seconds)) return null;
  return Math.min(MAX_RETRY_S, Math.max(1, Math.ceil(seconds)));
}

const MESSAGES: Readonly<Record<string, string>> = {
  unauthorized:
    'The guide service did not accept your sign-in. Sign in again, or check the token in Settings.',
  origin_not_allowed:
    'The guide service does not accept requests from this app (guide.http.allowed_origins).',
  cross_site: 'The guide service refused a cross-site request.',
  csrf: 'The guide service refused the request: the session check failed. Reload the page and try again.',
  rate: 'You asked the guide service too often in the last minute.',
  concurrent: 'Another exploration of yours is still running.',
  busy: 'The guide service is busy.',
  keeper_unavailable: 'The guide service could not reach Keeper.',
  question_too_long: 'The question is longer than the guide service allows.',
};

async function refusal(response: Response): Promise<GuideError> {
  let code = '';
  try {
    const body: unknown = await response.json();
    if (typeof body === 'object' && body !== null) {
      const value = (body as Record<string, unknown>)['code'];
      if (typeof value === 'string') code = value;
    }
  } catch {
    // Not a problem document: the status alone explains it.
  }
  const retryAfterS =
    response.status === 429 ? (parseRetryAfter(response.headers.get('Retry-After')) ?? 1) : null;
  const message =
    MESSAGES[code] ??
    (response.status === 429
      ? MESSAGES['busy']
      : `The guide service answered ${response.status.toString()} to /explore.`);
  return new GuideError(message ?? '', response.status, code, retryAfterS);
}

/**
 * The live transport: `POST {guideUrl}/explore` with the caller's credential, read as a stream
 * of server-sent events. `guideUrl` is an absolute origin or a path on the app's own origin
 * (Guide behind the web proxy, as `/guide`); `auth` is read at call time.
 */
export function liveTransport(guideUrl: string, auth: () => GuideAuth): ExploreTransport {
  return async function* explore(request, signal) {
    const { headers, credentials } = auth();
    let response: Response;
    try {
      response = await fetch(`${guideUrl}/explore`, {
        method: 'POST',
        headers: { ...headers, 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        credentials,
        cache: 'no-store',
        body: JSON.stringify(request),
        signal,
      });
    } catch (error) {
      if (signal.aborted) throw error;
      throw new GuideError('The guide service could not be reached.', null, 'network');
    }
    if (!response.ok) throw await refusal(response);
    const type = response.headers.get('Content-Type') ?? '';
    if (!type.startsWith('text/event-stream') || response.body === null) {
      throw new GuideError(
        'The guide service did not answer with a stream.',
        response.status,
        'stream',
      );
    }
    for await (const message of readSse(response.body)) {
      const event = parseExploreEvent(message);
      if (event !== null) yield event;
    }
  };
}
