/** Source runs. Keeper does not publish them yet (design 11.1 lists them); the mock serves them. */

export type SourceRunStatus = 'queued' | 'running' | 'succeeded' | 'failed';

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
  trigger: 'schedule' | 'manual';
  started_at: string;
  finished_at: string | null;
  rows_read: number;
  changes: SourceRunChanges;
  /** Branch the run committed to. */
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
