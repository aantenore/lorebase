import { RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppProviders } from '@/app/providers';
import { ConfigError, loadConfig, type AppConfig } from '@/config/env';
import { createAppRouter } from './router';
import './styles.css';

function bootConfig(): AppConfig | ConfigError {
  try {
    return loadConfig(import.meta.env);
  } catch (error) {
    if (error instanceof ConfigError) return error;
    throw error;
  }
}

const container = document.getElementById('root');
if (container === null) throw new Error('Missing #root element.');

const config = bootConfig();

createRoot(container).render(
  <StrictMode>
    {config instanceof ConfigError ? (
      <main className="mx-auto max-w-xl p-8">
        <h1 className="text-lg font-semibold">Configuration error</h1>
        <p className="mt-2 text-danger">{config.message}</p>
      </main>
    ) : (
      <AppProviders config={config}>
        <RouterProvider router={createAppRouter()} />
      </AppProviders>
    )}
  </StrictMode>,
);
