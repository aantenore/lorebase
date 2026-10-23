import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';

const V = retrievalLabMessages.vector;

/**
 * Client of the optional guide service (`guideUrl` in the app configuration).
 *
 * `POST {guideUrl}/embed` with the JSON body `{"text": string, "mode": "query"}` answers
 * `{"vector": number[], "model"?: string, "dims"?: number}`: the embedding of the text, in the
 * model and dimension the Domain's search is configured with. The Retrieval lab sends it to
 * Keeper as `query_vector`. The service is not part of this app and may be absent.
 */
export interface EmbedAnswer {
  vector: number[];
  model?: string | undefined;
  dims?: number | undefined;
}

/** Reads a vector typed or pasted as a JSON array of numbers. */
export function parseVector(text: string): { vector: number[] | null; error: string | null } {
  if (text.trim() === '') return { vector: null, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { vector: null, error: V.notJson };
  }
  if (
    !Array.isArray(parsed) ||
    parsed.length === 0 ||
    !parsed.every((n) => typeof n === 'number' && Number.isFinite(n))
  ) {
    return { vector: null, error: V.notVector };
  }
  return { vector: parsed as number[], error: null };
}

/** Validates the answer of `/embed`. */
export function readEmbedAnswer(body: unknown): EmbedAnswer {
  const record = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {};
  const { vector, error } = parseVector(JSON.stringify(record['vector'] ?? null));
  if (vector === null) {
    throw new Error(error ?? V.noVector);
  }
  const model = record['model'];
  const dims = record['dims'];
  return {
    vector,
    model: typeof model === 'string' ? model : undefined,
    dims: typeof dims === 'number' ? dims : undefined,
  };
}

/**
 * How the caller authenticates to Guide: Guide accepts Keeper's credentials and forwards them to
 * Keeper unchanged (docs/guide.md). Token mode sends the same `Authorization` header as the
 * Keeper adapter; session mode sends the session cookie (Guide behind the app's origin) and the
 * CSRF header.
 */
export interface GuideAuth {
  headers: Record<string, string>;
  credentials: RequestCredentials;
}

export async function embed(
  guideUrl: string,
  text: string,
  auth: GuideAuth,
  signal?: AbortSignal,
): Promise<EmbedAnswer> {
  let response: Response;
  try {
    response = await fetch(`${guideUrl}/embed`, {
      method: 'POST',
      headers: {
        ...auth.headers,
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      credentials: auth.credentials,
      body: JSON.stringify({ text, mode: 'query' }),
      ...(signal === undefined ? {} : { signal }),
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new Error(V.unreachable, { cause: error });
  }
  if (!response.ok) {
    throw new Error(V.answered(response.status));
  }
  return readEmbedAnswer(await response.json());
}
