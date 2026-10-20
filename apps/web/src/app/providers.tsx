import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { createKeeperApi, type KeeperApi } from '@/api';
import { ApiProvider } from '@/api/provider';
import { ConfigProvider } from '@/config/context';
import type { AppConfig } from '@/config/env';
import { ThemeProvider } from '@/ui/theme';

interface AppProvidersProps {
  config: AppConfig;
  /** Override the adapter selected by `config.apiMode`; used by tests. */
  api?: KeeperApi;
  children: ReactNode;
}

export function AppProviders({ config, api, children }: AppProvidersProps) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: config.queryStaleMs, retry: false, refetchOnWindowFocus: false },
        },
      }),
  );
  const [adapter] = useState(() => api ?? createKeeperApi(config));

  return (
    <ConfigProvider config={config}>
      <ThemeProvider>
        <ApiProvider api={adapter}>
          <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
        </ApiProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
