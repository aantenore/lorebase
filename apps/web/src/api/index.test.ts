import { loadConfig } from '@/config/env';
import { createKeeperApi } from '.';

describe('createKeeperApi', () => {
  it('serves sample data in mock mode', async () => {
    const api = createKeeperApi(loadConfig({ VITE_API_MODE: 'mock' }));
    const domains = await api.domains.list();
    expect(domains.items.length).toBeGreaterThan(0);
  });

  it('calls the configured Keeper in live mode', async () => {
    const calls: string[] = [];
    const original = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL) => {
      calls.push(input instanceof Request ? input.url : input.toString());
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], next_cursor: null }), {
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }) satisfies typeof fetch;
    try {
      const api = createKeeperApi(
        loadConfig({ VITE_API_MODE: 'live', VITE_KEEPER_BASE_URL: 'https://keeper.example.test' }),
      );
      await api.domains.list();
    } finally {
      globalThis.fetch = original;
    }
    expect(calls).toEqual(['https://keeper.example.test/api/v1/domains']);
  });
});
