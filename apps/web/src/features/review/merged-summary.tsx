import type { ReactNode } from 'react';
import { useRevision } from '@/api/queries';
import type { MergeAttempt, RevisionId } from '@/api/types';
import { Badge, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { CODE_LABEL, locationOf, partitionName, type Names } from './conflict-meta';
import { countByChoice } from './conflict-state';

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="font-mono text-xs break-all">{children}</dd>
    </div>
  );
}

/** The merge Revision a Proposal produced, and what was resolved to get there. */
export function MergedSummary({
  domain,
  revisionId,
  attempt,
  names,
}: {
  domain: string;
  revisionId: RevisionId;
  attempt: MergeAttempt;
  names: Names;
}) {
  const revision = useRevision(domain, revisionId);
  return (
    <section
      aria-label="Merged Revision"
      className="mb-4 flex flex-col gap-3 rounded-md border border-success bg-surface p-4"
    >
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        Merged Revision <Badge tone="success">merged</Badge>
      </h3>
      <QueryBoundary query={revision}>
        {(data) => (
          <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Fact label="Revision">{data.id}</Fact>
            <Fact label="Parents">
              {data.parents
                .map((parent) =>
                  parent === attempt.ours
                    ? `${parent} (target)`
                    : parent === attempt.theirs
                      ? `${parent} (source)`
                      : parent,
                )
                .join(', ')}
            </Fact>
            <Fact label="Author">{data.author.principal}</Fact>
            <Fact label="Changes">
              {data.stats.changes} in {data.stats.partitions_changed} partitions
            </Fact>
          </dl>
        )}
      </QueryBoundary>

      <div className="flex flex-wrap gap-2 text-sm">
        {countByChoice(attempt).map(([choice, count]) => (
          <Badge key={choice}>
            {count} {choice}
          </Badge>
        ))}
        {attempt.auto_resolutions.length > 0 && (
          <Badge tone="accent">{attempt.auto_resolutions.length} by policy</Badge>
        )}
      </div>

      {attempt.conflicts.length > 0 && (
        <Table caption="Resolutions applied">
          <THead>
            <TR>
              <TH>Conflict</TH>
              <TH>Location</TH>
              <TH>Choice</TH>
              <TH>Resolver</TH>
            </TR>
          </THead>
          <TBody>
            {attempt.conflicts.map((conflict) => {
              const stored = attempt.resolutions.find(
                (r) => r.fingerprint === conflict.fingerprint,
              );
              return (
                <TR key={conflict.fingerprint}>
                  <TD>
                    {conflict.kind} {CODE_LABEL[conflict.kind]}
                  </TD>
                  <TD className="font-mono text-xs">{locationOf(conflict, names)}</TD>
                  <TD>{stored?.choice ?? '-'}</TD>
                  <TD>{stored?.resolver ?? '-'}</TD>
                </TR>
              );
            })}
            {attempt.auto_resolutions.map((auto) => (
              <TR key={auto.fingerprint}>
                <TD>
                  {auto.kind} {CODE_LABEL[auto.kind]}
                </TD>
                <TD className="font-mono text-xs">
                  {partitionName(auto.partition, names)}
                  {auto.key === null ? '' : `/${auto.key}`}
                </TD>
                <TD>{auto.choice}</TD>
                <TD>{auto.resolver}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </section>
  );
}
