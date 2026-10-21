import type { SourceRun } from '../../sources-types';

/** Runs of the Sources of each sample Domain, newest first (run 6 opened Proposal 40). */
export function buildSourceRuns(): Record<string, SourceRun[]> {
  return {
    supply: [
      {
        id: 'run_7',
        source: 'erp.vendors',
        status: 'succeeded',
        trigger: 'schedule',
        started_at: '2026-10-19T02:00:00Z',
        finished_at: '2026-10-19T02:04:11Z',
        rows_read: 31,
        changes: { added: 1, updated: 5, deleted: 0 },
        branch: 'main',
        proposal: null,
        error: null,
      },
      {
        id: 'run_6',
        source: 'erp.vendors',
        status: 'succeeded',
        trigger: 'schedule',
        started_at: '2026-10-12T02:00:00Z',
        finished_at: '2026-10-12T02:03:40Z',
        rows_read: 30,
        changes: { added: 0, updated: 4, deleted: 0 },
        branch: 'source/erp.vendors',
        proposal: '40',
        error: null,
      },
      {
        id: 'run_5',
        source: 'erp.vendors',
        status: 'failed',
        trigger: 'schedule',
        started_at: '2026-10-11T02:00:00Z',
        finished_at: '2026-10-11T02:00:07Z',
        rows_read: 0,
        changes: { added: 0, updated: 0, deleted: 0 },
        branch: null,
        proposal: null,
        error: 'Connection to secret://env/ERP_DSN timed out after 5 s.',
      },
    ],
    hr: [],
  };
}
