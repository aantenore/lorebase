import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState, type ReactNode } from 'react';
import {
  createCredentials,
  createKeeperApi,
  createSessionClient,
  describeError,
  type KeeperApi,
  type SessionClient,
} from '@/api';
import { ApiProvider } from '@/api/provider';
import { LOGIN_PATH, redirectToLogin } from '@/api/session';
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

type SessionState = 'loading' | 'ready' | 'failed';

/**
 * Holds the app back until Keeper has said whether the browser has a session; without one it
 * goes to the sign-in screen. The sign-in screen itself renders at once.
 */
function SessionGate({ session, children }: { session: SessionClient; children: ReactNode }) {
  const [state, setState] = useState<SessionState>('loading');
  const [message, setMessage] = useState('');
  useEffect(() => {
    let active = true;
    session
      .load()
      .then((info) => {
        if (!active) return;
        if (info === null) redirectToLogin();
        else setState('ready');
      })
      .catch((error: unknown) => {
        if (!active) return;
        setMessage(error instanceof Error ? error.message : 'The request failed.');
        setState('failed');
      });
    return () => {
      active = false;
    };
  }, [session]);

  if (window.location.pathname === LOGIN_PATH) return children;
  if (state === 'ready') return children;
  if (state === 'failed') {
    return (
      <main className="mx-auto max-w-xl p-8">
        <h1 className="text-lg font-semibold">Cannot reach the sign-in service</h1>
        <p className="mt-2 text-danger">{message}</p>
      </main>
    );
  }
  return (
    <p role="status" className="p-8 text-sm text-muted">
      Checking your session...
    </p>
  );
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
          // A mutation with `meta.inlineError` shows its failure on the screen instead.
          onError: (error, _variables, _context, mutation) => {
            if (mutation.meta?.['inlineError'] !== true) reporter.report(error);
          },
        }),
        defaultOptions: {
          queries: { staleTime: config.queryStaleMs, retry: false, refetchOnWindowFocus: false },
        },
      }),
  );
  const [credentials] = useState(() => createCredentials(config.apiToken, browserStorage()));
  const [session] = useState(() =>
    config.apiMode === 'live' && config.authMode === 'session' && config.keeperBaseUrl !== null
      ? createSessionClient({ baseUrl: config.keeperBaseUrl })
      : null,
  );
  const [adapter] = useState(() => api ?? createKeeperApi(config, credentials, session));
  const content =
    session === null ? children : <SessionGate session={session}>{children}</SessionGate>;

  return (
    <ConfigProvider config={config}>
      <ThemeProvider>
        <ApiProvider api={adapter} credentials={credentials} session={session}>
          <QueryClientProvider client={queryClient}>
            <ToastProvider durationMs={config.toastMs}>
              <ErrorToasts reporter={reporter} />
              {content}
            </ToastProvider>
          </QueryClientProvider>
        </ApiProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
