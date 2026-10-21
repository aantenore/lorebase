import { createContext, use, type ReactNode } from 'react';
import type { KeeperApi } from './client';
import type { Credentials } from './credentials';

const ApiContext = createContext<KeeperApi | null>(null);
const CredentialsContext = createContext<Credentials | null>(null);

export function ApiProvider({
  api,
  credentials,
  children,
}: {
  api: KeeperApi;
  credentials: Credentials;
  children: ReactNode;
}) {
  return (
    <ApiContext value={api}>
      <CredentialsContext value={credentials}>{children}</CredentialsContext>
    </ApiContext>
  );
}

export function useKeeperApi(): KeeperApi {
  const api = use(ApiContext);
  if (api === null) throw new Error('useKeeperApi must be used inside ApiProvider.');
  return api;
}

/** The API token of this browser session (live mode); see `createCredentials`. */
export function useCredentials(): Credentials {
  const credentials = use(CredentialsContext);
  if (credentials === null) throw new Error('useCredentials must be used inside ApiProvider.');
  return credentials;
}
