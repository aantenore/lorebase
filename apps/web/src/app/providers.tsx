import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import { createCredentials, createKeeperApi, describeError, type KeeperApi } from '@/api';
import { ApiProvider } from '@/api/provider';
import { ConfigProvider } from '@/config/context';
import type { AppConfig } from '@/config/env';
import { ThemeProvider } from '@/ui/theme';
import { ToastProvider, useToast } from '@/ui/toast';

interface AppProvidersProps {
  config: AppConfig;
  /** Override the adapter selected by `config.apiMode`; used by tests. */
  api?: KeeperApi;
  children: ReactNode;
}

function browserStorage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Hands failures of the query client to whoever shows them (the toasts, once mounted). */
function createErrorReporter() {
  let show: (message: string) => void = () => undefined;
  return {
    report: (error: unknown) => {
      show(describeError(error));
    },
    bind: (next: (message: string) => void) => {
      show = next;
    },
  };
}

/** Shows every failed read and write as a toast, so a screen need not repeat that. */
function ErrorToasts({ reporter }: { reporter: ReturnType<typeof createErrorReporter> }) {
  const toast = useToast();
  useEffect(() => {
    reporter.bind((message) => {
      toast.notify(message, 'error');
    });
  }, [reporter, toast]);
  return null;
}

export function AppProviders({ config, api, children }: AppProvidersProps) {
  const [reporter] = useState(createErrorReporter);
  const [queryClient] = useState(
    () =>
      new QueryClient({
        queryCache: new QueryCache({
          onError: reporter.report,
        }),
        mutationCache: new MutationCache({
          onError: reporter.report,
        }),
        defaultOptions: {
          queries: { staleTime: config.queryStaleMs, retry: false, refetchOnWindowFocus: false },
        },
      }),
  );
  const [credentials] = useState(() => createCredentials(config.apiToken, browserStorage()));
  const [adapter] = useState(() => api ?? createKeeperApi(config, credentials));

  return (
    <ConfigProvider config={config}>
      <ThemeProvider>
        <ApiProvider api={adapter} credentials={credentials}>
          <QueryClientProvider client={queryClient}>
            <ToastProvider durationMs={config.toastMs}>
              <ErrorToasts reporter={reporter} />
              {children}
            </ToastProvider>
          </QueryClientProvider>
        </ApiProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
