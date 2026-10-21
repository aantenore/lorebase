import type { KeeperApi } from '../client';
import { notFound } from '../errors';
import type { Page } from '../types';
import type { SourceRun } from '../sources-types';
import { decodeCursor, encodeCursor } from './engine';
import { buildSourceRuns } from './sample/sources';

export interface SourcesMock {
  api: KeeperApi['sources'];
}

/**
 * Source runs in memory. A manual sync reads the Source again and finds nothing new: it
 * succeeds at once with no changes and opens no Proposal (the sample rows do not move).
 */
export function createSourcesMock(
  sourcesOf: (domain: string) => string[],
  onRun: (domain: string, run: SourceRun) => void,
  seed: Record<string, SourceRun[]> = buildSourceRuns(),
): SourcesMock {
  const runs = new Map(Object.entries(seed));
  let counter = 100;

  const api: KeeperApi['sources'] = {
    runs: (domain, params) => {
      const all = (runs.get(domain) ?? []).filter(
        (r) => params?.source === undefined || r.source === params.source,
      );
      const offset = decodeCursor(params?.cursor);
      const limit = params?.limit;
      const items = limit === undefined ? all.slice(offset) : all.slice(offset, offset + limit);
      const next =
        limit !== undefined && offset + limit < all.length ? encodeCursor(offset + limit) : null;
      return Promise.resolve<Page<SourceRun>>({ items: structuredClone(items), next_cursor: next });
    },
    sync: (domain, source) => {
      if (!sourcesOf(domain).includes(source)) return Promise.reject(notFound(`Source ${source}`));
      counter += 1;
      const now = new Date().toISOString();
      const run: SourceRun = {
        id: `run_${counter.toString()}`,
        source,
        status: 'succeeded',
        trigger: 'manual',
        started_at: now,
        finished_at: now,
        rows_read: 31,
        changes: { added: 0, updated: 0, deleted: 0 },
        branch: null,
        proposal: null,
        error: null,
      };
      runs.set(domain, [run, ...(runs.get(domain) ?? [])]);
      onRun(domain, run);
      return Promise.resolve(structuredClone(run));
    },
  };
  return { api };
}
