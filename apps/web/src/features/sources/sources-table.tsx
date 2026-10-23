import type { SourceRun, SourceRunStatus } from '@/api/sources-types';
import type { Schema } from '@/api/types';
import { sources as sourcesMessages } from '@/messages/sources';
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
    <Table caption={sourcesMessages.table.caption}>
      <THead>
        <TR>
          <TH>{sourcesMessages.table.source}</TH>
          <TH>{sourcesMessages.table.type}</TH>
          <TH>{sourcesMessages.table.mapping}</TH>
          <TH>{sourcesMessages.table.policy}</TH>
          <TH>{sourcesMessages.table.schedule}</TH>
          <TH>{sourcesMessages.table.lastRun}</TH>
          <TH>{sourcesMessages.table.lastRunCounts}</TH>
          <TH>{sourcesMessages.table.actions}</TH>
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
              <TD>{source.schedule ?? sourcesMessages.table.manual}</TD>
              <TD>
                {runs === undefined ? (
                  <span className="text-muted">{sourcesMessages.table.unavailable}</span>
                ) : last === undefined ? (
                  <span className="text-muted">{sourcesMessages.table.neverRun}</span>
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
                    {sourcesMessages.table.rows(last.rows_read)}
                    <div className="font-mono text-xs text-muted">{changesLabel(last)}</div>
                  </>
                )}
              </TD>
              <TD>
                <Button
                  size="sm"
                  className="whitespace-nowrap"
                  aria-label={sourcesMessages.table.syncLabel(source.api_name)}
                  disabled={syncing !== null}
                  onClick={() => {
                    onSync(source.api_name);
                  }}
                >
                  {syncing === source.api_name
                    ? sourcesMessages.table.syncing
                    : sourcesMessages.table.sync}
                </Button>
              </TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}
