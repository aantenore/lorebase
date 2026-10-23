import { Link } from '@tanstack/react-router';
import type { SourceRun } from '@/api/sources-types';
import { useScopeSearch } from '@/app/scope';
import { sources as sourcesMessages } from '@/messages/sources';
import { Badge } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { changesLabel } from './source-summary';
import { RUN_TONE } from './sources-table';

/** Run history, newest first, with the Proposal each run opened and the error of a failed one. */
export function RunsTable({ domain, runs }: { domain: string; runs: readonly SourceRun[] }) {
  const scopeSearch = useScopeSearch();
  return (
    <Table caption={sourcesMessages.runs.caption}>
      <THead>
        <TR>
          <TH>{sourcesMessages.runs.run}</TH>
          <TH>{sourcesMessages.runs.source}</TH>
          <TH>{sourcesMessages.runs.status}</TH>
          <TH>{sourcesMessages.runs.trigger}</TH>
          <TH>{sourcesMessages.runs.started}</TH>
          <TH>{sourcesMessages.runs.rowsRead}</TH>
          <TH>{sourcesMessages.runs.changes}</TH>
          <TH>{sourcesMessages.runs.branch}</TH>
          <TH>{sourcesMessages.runs.proposal}</TH>
        </TR>
      </THead>
      <TBody>
        {runs.map((run) => (
          <TR key={run.id}>
            <TD className="font-mono text-xs">{run.id}</TD>
            <TD>{run.source}</TD>
            <TD>
              <Badge tone={RUN_TONE[run.status]}>{run.status}</Badge>
              {run.error !== null && <p className="mt-1 text-xs text-danger">{run.error}</p>}
            </TD>
            <TD>{run.trigger ?? '-'}</TD>
            <TD>{shortTime(run.started_at)}</TD>
            <TD>{run.rows_read}</TD>
            <TD className="font-mono text-xs">{changesLabel(run)}</TD>
            <TD>{run.branch ?? '-'}</TD>
            <TD>
              {run.proposal === null ? (
                <span className="text-muted">{sourcesMessages.runs.noProposal}</span>
              ) : (
                <Link
                  to="/$domain/review/$proposal"
                  params={{ domain, proposal: run.proposal }}
                  search={scopeSearch}
                  className="text-accent hover:underline"
                >
                  #{run.proposal}
                </Link>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}
