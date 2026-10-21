import { urlOf } from '@/test/fetch';
import { loadConfig } from '@/config/env';
import { createKeeperApi } from '.';
import { createCredentials, TOKEN_STORAGE_KEY } from './credentials';

const memory = () => {
  const data = new Map<string, string>();
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
  };
};

describe('createKeeperApi', () => {
  it('serves sample data in mock mode', async () => {
    const api = createKeeperApi(
      loadConfig({ VITE_API_MODE: 'mock' }),
      createCredentials(null, null),
    );
    const domains = await api.domains.list();
    expect(domains.items.length).toBeGreaterThan(0);
  });

  it('calls the configured Keeper in live mode with the current token', async () => {
    const calls: { url: string; auth: string | undefined }[] = [];
    const original = globalThis.fetch;
    globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: urlOf(input),
        auth: (init?.headers as Record<string, string> | undefined)?.['Authorization'],
      });
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], next_cursor: null }), {
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }) satisfies typeof fetch;
    const credentials = createCredentials('from-env', memory());
    try {
      const api = createKeeperApi(
        loadConfig({
          VITE_API_MODE: 'live',
          VITE_KEEPER_BASE_URL: 'https://keeper.example.test',
          VITE_API_TOKEN: 'from-env',
        }),
        credentials,
      );
      await api.domains.list();
      credentials.set('typed');
      await api.domains.list();
      credentials.set(null);
      await api.domains.list();
    } finally {
      globalThis.fetch = original;
    }
    expect(calls.map((c) => c.url)).toEqual(
      Array(3).fill('https://keeper.example.test/api/v1/domains'),
    );
    expect(calls.map((c) => c.auth)).toEqual([
      'Bearer from-env',
      'Bearer typed',
      'Bearer from-env',
    ]);
  });
});

describe('credentials', () => {
  it('prefers the stored token, tells where it came from and survives blocked storage', () => {
    const storage = memory();
    const credentials = createCredentials('env-token', storage);
    expect(credentials.source()).toBe('env');
    const seen: string[] = [];
    credentials.subscribe(() => seen.push(credentials.source()));
    credentials.set('  typed  ');
    expect(credentials.token()).toBe('typed');
    expect(storage.getItem(TOKEN_STORAGE_KEY)).toBe('typed');
    expect(credentials.source()).toBe('storage');
    credentials.set('');
    expect(credentials.source()).toBe('env');
    expect(seen).toEqual(['storage', 'env']);

    const blocked = createCredentials(null, {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => {
        throw new Error('blocked');
      },
    });
    expect(() => {
      blocked.set('x');
    }).not.toThrow();
    expect(blocked.source()).toBe('none');
  });
});
