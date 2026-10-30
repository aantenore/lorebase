/**
 * An adapter that loads its implementation on the first method call, so the implementation's
 * module (and the data it carries) stays out of the entry chunk. Every member is reached by
 * name: `api.domains.list(...)` awaits `load()` once, then calls `domains.list` on the result.
 * Only methods are forwarded; the adapter is not thenable.
 */
export function lazyAdapter<T extends object>(load: () => Promise<T>): T {
  let loading: Promise<T> | undefined;
  const at = (path: readonly string[]): unknown =>
    new Proxy(() => undefined, {
      get: (_target, key) =>
        typeof key === 'string' && key !== 'then' ? at([...path, key]) : undefined,
      apply: async (_target, _this, args: unknown[]) => {
        let owner: unknown = await (loading ??= load());
        for (const key of path.slice(0, -1)) owner = (owner as Record<string, unknown>)[key];
        const method = (owner as Record<string, unknown>)[path.at(-1) ?? ''];
        if (typeof method !== 'function') throw new TypeError(`${path.join('.')} is not a method.`);
        return (method as (...a: unknown[]) => unknown).apply(owner, args);
      },
    });
  return at([]) as T;
}
