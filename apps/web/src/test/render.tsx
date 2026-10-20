import { createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { render } from '@testing-library/react';
import { AppProviders } from '@/app/providers';
import { testConfig, type AppConfig } from '@/config/env';
import { createAppRouter } from '@/router';

/** Renders the whole app at `path` using an in-memory history. */
export function renderApp(path = '/', config: AppConfig = testConfig()) {
  const history = createMemoryHistory({ initialEntries: [path] });
  const router = createAppRouter(history);
  const utils = render(
    <AppProviders config={config}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { router, ...utils };
}
