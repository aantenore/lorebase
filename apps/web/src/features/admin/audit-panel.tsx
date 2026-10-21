import { useMemo, useState } from 'react';
import { useAuditLog } from '@/api/queries';
import { useScope } from '@/app/scope';
import { useConfig } from '@/config/context';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { EmptyState, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { distinct, filterAudit, NO_FILTER, type AuditFilter } from './audit-filter';
import { usePrincipalNames } from './principal-names';

const HASH_SHOWN = 12;

/** Audit log of the Domain, newest first, with filters over the entries loaded so far. */
export function AuditPanel() {
  const scope = useScope();
  const { pageSize } = useConfig();
  const log = useAuditLog(scope.domain, pageSize);
  const nameOf = usePrincipalNames();
  const [filter, setFilter] = useState<AuditFilter>(NO_FILTER);

  const loaded = useMemo(() => log.data?.pages.flatMap((page) => page.items) ?? [], [log.data]);
  const shown = useMemo(() => filterAudit(loaded, filter), [loaded, filter]);
  const set = (change: Partial<AuditFilter>) => {
    setFilter((current) => ({ ...current, ...change }));
  };
  const menu = (values: string[], all: string) => [
    { value: '', label: all },
    ...values.map((value) => ({ value, label: value })),
  ];

  return (
    <Section title={`Audit log of ${scope.domainInfo.display_name}`}>
      <p className="mb-3 text-sm text-muted">
        Keeper pages the log and filters nothing, so the filters apply to the {loaded.length}{' '}
        entries loaded so far.
      </p>
      <div className="mb-3 flex flex-wrap items-end gap-3">
        <div className="w-48">
          <Select
            label="Actor"
            value={filter.actor}
            options={menu(distinct(loaded, 'actor'), 'All actors')}
            onChange={(event) => {
              set({ actor: event.target.value });
            }}
          />
        </div>
        <div className="w-56">
          <Select
            label="Action"
            value={filter.action}
            options={menu(distinct(loaded, 'action'), 'All actions')}
            onChange={(event) => {
              set({ action: event.target.value });
            }}
          />
        </div>
        <div className="w-64">
          <Input
            label="Contains"
            value={filter.text}
            placeholder="target, actor or detail"
            onChange={(event) => {
              set({ text: event.target.value });
            }}
          />
        </div>
        <Button
          disabled={Object.values(filter).every((value) => value === '')}
          onClick={() => {
            setFilter(NO_FILTER);
          }}
        >
          Clear filters
        </Button>
      </div>

      <QueryBoundary query={log}>
        {() =>
          shown.length === 0 ? (
            <EmptyState>
              {loaded.length === 0
                ? 'The audit log is empty.'
                : 'No loaded entry matches the filters.'}
            </EmptyState>
          ) : (
            <Table caption="Audit log">
              <THead>
                <TR>
                  <TH>Seq</TH>
                  <TH>Time</TH>
                  <TH>Actor</TH>
                  <TH>Action</TH>
                  <TH>Target</TH>
                  <TH>Details</TH>
                  <TH>Hash</TH>
                </TR>
              </THead>
              <TBody>
                {shown.map((entry) => (
                  <TR key={entry.seq}>
                    <TD>{entry.seq}</TD>
                    <TD>{shortTime(entry.at)}</TD>
                    <TD>{nameOf(entry.actor)}</TD>
                    <TD>
                      <code className="font-mono text-xs">{entry.action}</code>
                    </TD>
                    <TD>{entry.target ?? '-'}</TD>
                    <TD className="font-mono text-xs break-all">
                      {Object.keys(entry.details).length === 0
                        ? '-'
                        : JSON.stringify(entry.details)}
                    </TD>
                    <TD className="font-mono text-xs" title={entry.entry_hash}>
                      {entry.entry_hash.slice(0, HASH_SHOWN)}
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )
        }
      </QueryBoundary>
      {log.hasNextPage && (
        <div className="mt-3">
          <Button
            disabled={log.isFetchingNextPage}
            onClick={() => {
              void log.fetchNextPage();
            }}
          >
            {log.isFetchingNextPage ? 'Loading...' : 'Load older entries'}
          </Button>
        </div>
      )}
    </Section>
  );
}
