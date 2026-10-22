import { createContext, use, useSyncExternalStore, type ReactNode } from 'react';
import type { KeeperApi } from './client';
import type { Credentials } from './credentials';
import type { SessionClient, SessionInfo } from './session';

const ApiContext = createContext<KeeperApi | null>(null);
const CredentialsContext = createContext<Credentials | null>(null);
const SessionContext = createContext<SessionClient | null>(null);

export function ApiProvider({
  api,
  credentials,
  session = null,
  children,
}: {
  api: KeeperApi;
  credentials: Credentials;
  session?: SessionClient | null;
  children: ReactNode;
}) {
  return (
    <ApiContext value={api}>
      <CredentialsContext value={credentials}>
        <SessionContext value={session}>{children}</SessionContext>
      </CredentialsContext>
    </ApiContext>
  );
}

export function useKeeperApi(): KeeperApi {
  const api = use(ApiContext);
  if (api === null) throw new Error('useKeeperApi must be used inside ApiProvider.');
  return api;
}

/** The Keeper API when there is one: for screens that also work without it (tests). */
export function useOptionalKeeperApi(): KeeperApi | null {
  return use(ApiContext);
}

/** The API token of this browser session (live mode); see `createCredentials`. */
export function useCredentials(): Credentials {
  const credentials = use(CredentialsContext);
  if (credentials === null) throw new Error('useCredentials must be used inside ApiProvider.');
  return credentials;
}

/** The browser session client in session auth mode (live), `null` otherwise. */
export function useSession(): SessionClient | null {
  return use(SessionContext);
}

const noSubscription = () => () => undefined;
const noSession = () => null;

/** The signed-in session as last loaded; `null` without a session or outside session mode. */
export function useSessionInfo(): SessionInfo | null {
  const session = use(SessionContext);
  return useSyncExternalStore(session?.subscribe ?? noSubscription, session?.info ?? noSession);
}
