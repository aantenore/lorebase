import { createContext, use, type ReactNode } from 'react';
import type { AppConfig } from './env';

const ConfigContext = createContext<AppConfig | null>(null);

export function ConfigProvider({ config, children }: { config: AppConfig; children: ReactNode }) {
  return <ConfigContext value={config}>{children}</ConfigContext>;
}

export function useConfig(): AppConfig {
  const config = use(ConfigContext);
  if (config === null) throw new Error('useConfig must be used inside ConfigProvider.');
  return config;
}

/** The configuration, or `null` outside a ConfigProvider (shared UI primitives in isolation). */
export function useOptionalConfig(): AppConfig | null {
  return use(ConfigContext);
}
