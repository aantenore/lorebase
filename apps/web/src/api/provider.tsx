import { createContext, use, type ReactNode } from 'react';
import type { KeeperApi } from './client';

const ApiContext = createContext<KeeperApi | null>(null);

export function ApiProvider({ api, children }: { api: KeeperApi; children: ReactNode }) {
  return <ApiContext value={api}>{children}</ApiContext>;
}

export function useKeeperApi(): KeeperApi {
  const api = use(ApiContext);
  if (api === null) throw new Error('useKeeperApi must be used inside ApiProvider.');
  return api;
}
