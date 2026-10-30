import { lazyAdapter } from './lazy';

describe('lazyAdapter', () => {
  it('loads once on the first call and forwards nested methods with their owner', async () => {
    let loads = 0;
    const api = lazyAdapter(() => {
      loads += 1;
      const domains = {
        prefix: 'd:',
        get(id: string) {
          return Promise.resolve(`${this.prefix}${id}`);
        },
      };
      return Promise.resolve({ domains });
    });
    expect(loads).toBe(0);
    await expect(api.domains.get('a')).resolves.toBe('d:a');
    await expect(api.domains.get('b')).resolves.toBe('d:b');
    expect(loads).toBe(1);
  });

  it('is not thenable and rejects calls of what is not a method', async () => {
    const api = lazyAdapter(() => Promise.resolve({ domains: { count: 1 } }));
    expect((api as { then?: unknown }).then).toBeUndefined();
    await expect(Promise.resolve(api)).resolves.toBe(api);
    const count = (api.domains as unknown as { count: () => Promise<number> }).count;
    await expect(count()).rejects.toThrow('domains.count is not a method.');
  });
});
