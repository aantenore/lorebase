import { API_BASE_PATH } from './fetcher';
import type { components } from './generated/keeper';

/** What Keeper reports about the browser session (`GET /auth/session`). */
export type SessionInfo = components['schemas']['SessionInfo'];
type LogoutResult = components['schemas']['LogoutResult'];

export interface SessionClient {
  /** Absolute Keeper URL that starts the login and returns to `returnTo` (a path of this app). */
  loginUrl: (returnTo: string) => string;
  /** Ask Keeper for the current session; `null` when there is none (401). */
  load: () => Promise<SessionInfo | null>;
  /** The session as last loaded, without a request. */
  info: () => SessionInfo | null;
  /** The CSRF header for unsafe methods; empty for GET and HEAD or without a session. */
  csrfHeaders: (method: string) => Record<string, string>;
  /** End the session; resolves to the provider's end-session URL, or `null`. */
  logout: () => Promise<string | null>;
  subscribe: (listener: () => void) => () => void;
}

export interface SessionClientOptions {
  /** Keeper origin without the API path (empty for the app origin). */
  baseUrl: string;
  /** Injected for tests; defaults to the global fetch. */
  fetch?: typeof fetch;
}

const SAFE_METHODS = new Set(['GET', 'HEAD']);

/** A return target must be a path of this app: one leading slash, no scheme-relative form. */
export function safeReturnTo(value: string): string {
  return value.startsWith('/') && !value.startsWith('//') && !value.startsWith('/\\') ? value : '/';
}

/** App path of the sign-in screen. */
export const LOGIN_PATH = '/login';

/** Send the browser to the sign-in screen, remembering where it was; no-op when already there. */
export function redirectToLogin(): void {
  const { pathname, search } = window.location;
  if (pathname === LOGIN_PATH) return;
  window.location.assign(`${LOGIN_PATH}?return_to=${encodeURIComponent(`${pathname}${search}`)}`);
}

export function createSessionClient(options: SessionClientOptions): SessionClient {
  const root = `${options.baseUrl.replace(/\/+$/, '')}${API_BASE_PATH}/auth`;
  const doFetch: typeof fetch = options.fetch ?? ((input, init) => fetch(input, init));
  const listeners = new Set<() => void>();
  let current: SessionInfo | null = null;

  const set = (next: SessionInfo | null) => {
    current = next;
    listeners.forEach((listener) => {
      listener();
    });
  };

  const csrfHeaders = (method: string): Record<string, string> =>
    current === null || SAFE_METHODS.has(method.toUpperCase())
      ? {}
      : { [current.csrf_header]: current.csrf_token };

  return {
    loginUrl: (returnTo) => `${root}/login?return_to=${encodeURIComponent(safeReturnTo(returnTo))}`,
    load: async () => {
      const response = await doFetch(`${root}/session`, {
        headers: { Accept: 'application/json' },
        credentials: 'include',
      });
      if (response.status === 401) {
        set(null);
        return null;
      }
      if (!response.ok) throw new Error(`Keeper answered ${response.status} for the session.`);
      const info = (await response.json()) as SessionInfo;
      set(info);
      return info;
    },
    info: () => current,
    csrfHeaders,
    logout: async () => {
      const response = await doFetch(`${root}/logout`, {
        method: 'POST',
        headers: { Accept: 'application/json', ...csrfHeaders('POST') },
        credentials: 'include',
      });
      if (!response.ok) throw new Error(`Keeper answered ${response.status} for the sign-out.`);
      const body = (await response.json()) as LogoutResult | null;
      set(null);
      return body?.logout_url ?? null;
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
