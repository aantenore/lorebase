import type { ReactNode } from 'react';
import { useRevision } from '@/api/queries';
import type { MergeAttempt, RevisionId } from '@/api/types';
import { review as reviewMessages } from '@/messages/review';
import { Badge, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { CODE_LABEL, locationOf, partitionName, policyReason, type Names } from './conflict-meta';
import { countByChoice } from './conflict-state';

const text = reviewMessages.summary;

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
      aria-label={text.label}
      className="flex min-w-0 flex-col gap-4 rounded-md border border-success bg-surface p-4"
    >
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        {text.title} <Badge tone="success">{text.badge}</Badge>
      </h3>
      <QueryBoundary query={revision}>
        {(data) => (
          <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Fact label={text.revision}>{data.id}</Fact>
            <Fact label={text.parents}>
              {data.parents
                .map((parent) =>
                  parent === attempt.ours
                    ? `${parent} (${text.target})`
                    : parent === attempt.theirs
                      ? `${parent} (${text.source})`
                      : parent,
                )
                .join(', ')}
            </Fact>
            <Fact label={text.author}>{data.author.principal}</Fact>
            <Fact label={text.changes}>
              {text.changeStats(data.stats.changes, data.stats.partitions_changed)}
            </Fact>
          </dl>
        )}
      </QueryBoundary>

      <div className="flex flex-wrap gap-2 text-sm">
        {countByChoice(attempt).map(([choice, count]) => (
          <Badge key={choice}>{text.count(count, choice)}</Badge>
        ))}
        {attempt.auto_resolutions.length > 0 && (
          <Badge tone="accent">
            {reviewMessages.attempt.byPolicy(attempt.auto_resolutions.length)}
          </Badge>
        )}
      </div>

      {attempt.conflicts.length > 0 && (
        <Table caption={text.caption}>
          <THead>
            <TR>
              <TH>{text.columns.conflict}</TH>
              <TH>{text.columns.location}</TH>
              <TH>{text.columns.choice}</TH>
              <TH>{text.columns.resolver}</TH>
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
                  <TD>{stored?.choice ?? text.none}</TD>
                  <TD>{stored?.resolver ?? text.none}</TD>
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
                <TD>
                  {auto.resolver}
                  {policyReason(auto) === null ? null : (
                    <span className="block text-xs text-muted">{policyReason(auto)}</span>
                  )}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
    </section>
  );
}
