import { createBrowserHistory, createRouter, type RouterHistory } from '@tanstack/react-router';
import { RouteFallback } from '@/ui/route-fallback';
import { routeTree } from './routeTree.gen';

/** How long a screen may load before the fallback shows, so fast loads do not flash it. */
const PENDING_MS = 150;

export function createAppRouter(history?: RouterHistory) {
  return createRouter({
    routeTree,
    history: history ?? createBrowserHistory(),
    defaultPreload: 'intent',
    // Route code loads on demand; show the fallback once a screen takes noticeably long.
    defaultPendingComponent: RouteFallback,
    defaultPendingMs: PENDING_MS,
  });
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
