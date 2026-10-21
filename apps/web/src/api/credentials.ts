/** Where the browser keeps the API token typed in Settings. Development only (see README). */
export const TOKEN_STORAGE_KEY = 'lorebase.apiToken';

export type TokenSource = 'storage' | 'env' | 'none';

type TokenStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/**
 * The credential the live adapter sends as `Authorization: Bearer`. The token typed in
 * Settings (kept in local storage) wins over `VITE_API_TOKEN`. Both sit in the browser in the
 * clear, so this is a development convenience: production deployments put the app behind a
 * login or proxy that adds the header.
 */
export interface Credentials {
  token: () => string | null;
  source: () => TokenSource;
  /** Store a token, or forget the stored one with `null` (the `VITE_API_TOKEN` value remains). */
  set: (token: string | null) => void;
  subscribe: (listener: () => void) => () => void;
}

export function createCredentials(
  envToken: string | null,
  storage: TokenStorage | null,
): Credentials {
  const listeners = new Set<() => void>();
  const read = (): string | null => {
    try {
      const stored = storage?.getItem(TOKEN_STORAGE_KEY) ?? null;
      return stored === null || stored === '' ? null : stored;
    } catch {
      return null;
    }
  };

  return {
    token: () => read() ?? envToken,
    source: () => (read() !== null ? 'storage' : envToken !== null ? 'env' : 'none'),
    set: (token) => {
      try {
        if (token === null || token.trim() === '') storage?.removeItem(TOKEN_STORAGE_KEY);
        else storage?.setItem(TOKEN_STORAGE_KEY, token.trim());
      } catch {
        // Storage can be blocked; the token then stays what it was.
      }
      listeners.forEach((listener) => {
        listener();
      });
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
