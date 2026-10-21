import type { SourceRun, SourceRunStatus } from '@/api/sources-types';
import type { Schema } from '@/api/types';
import { Button } from '@/ui/button';
import { Badge, type BadgeTone } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { changesLabel, lastRunOf, mappingSummary, policyLabel } from './source-summary';

export const RUN_TONE: Record<SourceRunStatus, BadgeTone> = {
  succeeded: 'success',
  failed: 'danger',
  running: 'accent',
  queued: 'neutral',
  cancelled: 'neutral',
};

export interface SourcesTableProps {
  schema: Schema;
  /** Runs newest first; `undefined` while they load or when they could not be read. */
  runs: readonly SourceRun[] | undefined;
  /** Api name of the Source being synced, if any. */
  syncing: string | null;
  onSync: (source: string) => void;
}

/** The Sources of the schema with their mapping, policy, schedule and last run. */
export function SourcesTable({ schema, runs, syncing, onSync }: SourcesTableProps) {
  return (
    <Table caption="Sources">
      <THead>
        <TR>
          <TH>Source</TH>
          <TH>Type</TH>
          <TH>Mapping</TH>
          <TH>Policy</TH>
          <TH>Schedule</TH>
          <TH>Last run</TH>
          <TH>Last run counts</TH>
          <TH>Actions</TH>
        </TR>
      </THead>
      <TBody>
        {schema.sources.map((source) => {
          const last = runs === undefined ? undefined : lastRunOf(runs, source.api_name);
          return (
            <TR key={source.id}>
              <TD>
                <div className="font-medium">{source.api_name}</div>
                <div className="font-mono text-xs text-muted">{source.connection}</div>
              </TD>
              <TD>{source.type}</TD>
              <TD>{mappingSummary(source, schema)}</TD>
              <TD>{policyLabel(source)}</TD>
              <TD>{source.schedule ?? 'manual'}</TD>
              <TD>
                {runs === undefined ? (
                  <span className="text-muted">n/a</span>
                ) : last === undefined ? (
                  <span className="text-muted">never run</span>
                ) : (
                  <>
                    <Badge tone={RUN_TONE[last.status]}>{last.status}</Badge>
                    <div className="mt-1 text-xs text-muted">{shortTime(last.started_at)}</div>
                  </>
                )}
              </TD>
              <TD>
                {last === undefined ? (
                  '-'
                ) : (
                  <>
                    {last.rows_read} rows
                    <div className="font-mono text-xs text-muted">{changesLabel(last)}</div>
                  </>
                )}
              </TD>
              <TD>
                <Button
                  size="sm"
                  className="whitespace-nowrap"
                  aria-label={`Sync ${source.api_name} now`}
                  disabled={syncing !== null}
                  onClick={() => {
                    onSync(source.api_name);
                  }}
                >
                  {syncing === source.api_name ? 'Syncing...' : 'Sync now'}
                </Button>
              </TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}
