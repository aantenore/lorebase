import { createRootRoute, Outlet } from '@tanstack/react-router';

export const Route = createRootRoute({
  component: Outlet,
  notFoundComponent: () => (
    <main className="p-8">
      <h1 className="text-lg font-semibold">Page not found</h1>
    </main>
  ),
});
