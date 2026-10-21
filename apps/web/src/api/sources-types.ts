/** Source runs (design 9.1), as the Sources screen shows them. */
import type { components } from './generated/keeper';

export type SourceRunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface SourceRunChanges {
  added: number;
  updated: number;
  deleted: number;
}

export interface SourceRun {
  id: string;
  /** Api name of the Source. */
  source: string;
  status: SourceRunStatus;
  /** How the run started; `null` when Keeper does not say (it records no trigger). */
  trigger: 'schedule' | 'manual' | null;
  started_at: string | null;
  finished_at: string | null;
  rows_read: number;
  changes: SourceRunChanges;
  /** Branch the run wrote to. */
  branch: string | null;
  /** Proposal the run opened, when the merge policy asked for review. */
  proposal: string | null;
  error: string | null;
}

export interface SourceRunListParams {
  source?: string;
  limit?: number;
  cursor?: string | null;
}

/**
 * Answer of `POST .../sources/{source}/sync`: the queued job, run by Scribe. `job_id` is the
 * queued job, or the identical job already pending; `source` is the id of the Source.
 */
export type SourceSyncQueued = components['schemas']['SyncQueued'];
