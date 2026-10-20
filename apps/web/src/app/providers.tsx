import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { ConfigProvider } from '@/config/context';
import type { AppConfig } from '@/config/env';
import { ThemeProvider } from '@/ui/theme';

export function AppProviders({ config, children }: { config: AppConfig; children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: config.queryStaleMs, retry: false, refetchOnWindowFocus: false },
        },
      }),
  );

  return (
    <ConfigProvider config={config}>
      <ThemeProvider>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
