import type { KeeperApi } from '../client';
import { notFound } from '../errors';
import type { Page } from '../types';
import type { SourceRun, SourceSyncQueued } from '../sources-types';
import { decodeCursor, encodeCursor } from './engine';
import { buildSourceRuns } from './sample/sources';

export interface SourcesMock {
  api: Omit<KeeperApi['sources'], 'previewExpressions'>;
}

/**
 * Source runs in memory. A manual sync queues a job like Keeper does; the mock then runs it at
 * once: it reads the Source again and finds nothing new, so the run succeeds with no changes
 * and opens no Proposal (the sample rows do not move).
 */
export function createSourcesMock(
  sourcesOf: (domain: string) => { id: string; api_name: string }[],
  onRun: (domain: string, run: SourceRun) => void,
  seed: Record<string, SourceRun[]> = buildSourceRuns(),
): SourcesMock {
  const runs = new Map(Object.entries(seed));
  let counter = 100;

  const api: SourcesMock['api'] = {
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
      const found = sourcesOf(domain).find((s) => s.api_name === source);
      if (found === undefined) return Promise.reject(notFound(`Source ${source}`));
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
      return Promise.resolve<SourceSyncQueued>({
        job_id: counter,
        source: found.id,
        api_name: found.api_name,
        dry_run: false,
      });
    },
  };
  return { api };
}
