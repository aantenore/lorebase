import { useMemo } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ContextPack } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { cn } from '@/ui/cn';
import { Badge, EmptyState } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { budgetReport, explainPack, type BudgetReport } from './pack-explain';

function Budget({ report }: { report: BudgetReport }) {
  const percent = Math.min(100, Math.round(report.usage * 100));
  return (
    <div className="rounded-md border border-line bg-surface p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">
          {report.used} of {report.limit} tokens
        </span>
        <span aria-hidden="true" className="h-2 w-40 overflow-hidden rounded-full bg-surface-2">
          <span
            className={cn('block h-full', report.overBudget ? 'bg-danger' : 'bg-accent')}
            style={{ width: `${percent.toString()}%` }}
          />
        </span>
        {report.overBudget && <Badge tone="danger">over budget</Badge>}
        {report.capped && <Badge tone="warning">capped</Badge>}
      </div>
      <ul aria-label="Tokens per section" className="mt-2 flex flex-wrap gap-3 text-xs">
        {report.sections.map((section) => (
          <li key={section.name}>
            {section.name}: {section.tokens} ({Math.round(section.share * 100)}%)
          </li>
        ))}
      </ul>
      {(report.trimmed.length > 0 || report.omitted.length > 0) && (
        <p className="mt-2 text-xs text-warning">
          To fit the budget: {[...report.trimmed, ...report.omitted].join('; ')}.
        </p>
      )}
    </div>
  );
}

/** The context pack an agent would get for the question, and why each Record is in it. */
export function PackPanel({
  domain,
  question,
  query,
}: {
  domain: string;
  question: string | undefined;
  query: UseQueryResult<ContextPack>;
}) {
  const pack = query.data;
  const entries = useMemo(
    () => (pack === undefined ? [] : explainPack(pack, question ?? '')),
    [pack, question],
  );
  if (question === undefined) return <EmptyState>Ask a question to see its pack.</EmptyState>;
  if (query.isPending) {
    return (
      <p role="status" className="text-muted">
        Building the pack...
      </p>
    );
  }
  if (query.isError) {
    return (
      <p role="alert" className="text-danger">
        {query.error.message}
      </p>
    );
  }
  const links = Object.entries(query.data.links);
  return (
    <div className="flex flex-col gap-3">
      <Budget report={budgetReport(query.data.budget)} />
      {entries.length === 0 ? (
        <EmptyState>The search found no Record to seed the pack.</EmptyState>
      ) : (
        <Table caption="Records in the context pack">
          <THead>
            <tr>
              <TH>Record</TH>
              <TH>Role</TH>
              <TH>Why it is included</TH>
            </tr>
          </THead>
          <TBody>
            {entries.map((entry) => (
              <TR key={entry.ref}>
                <TD>
                  <RecordLink domain={domain} recordRef={entry.ref}>
                    {entry.title}
                  </RecordLink>
                  <span className="ml-2 font-mono text-xs text-muted">{entry.ref}</span>
                </TD>
                <TD>
                  <Badge tone={entry.role === 'seed' ? 'accent' : 'neutral'}>{entry.role}</Badge>
                </TD>
                <TD>
                  {entry.why}
                  {entry.valuesTrimmed && (
                    <span className="ml-1 text-xs text-warning">(values trimmed)</span>
                  )}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
      {links.length > 0 && (
        <details className="rounded-md border border-line bg-surface p-3 text-sm">
          <summary className="cursor-pointer font-medium">
            Links in the pack ({links.reduce((sum, [, list]) => sum + list.length, 0)})
          </summary>
          <ul className="mt-2 grid gap-1 text-xs sm:grid-cols-2">
            {links.flatMap(([relation, list]) =>
              list.map((link) => (
                <li key={`${relation}|${link.from}|${link.to}`}>
                  {link.from} <span className="text-muted">{relation}</span> {link.to}
                </li>
              )),
            )}
          </ul>
        </details>
      )}
    </div>
  );
}
