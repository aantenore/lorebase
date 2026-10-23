import { useMemo, useState } from 'react';
import { useAuditLog } from '@/api/queries';
import { useScope } from '@/app/scope';
import { useConfig } from '@/config/context';
import { admin as adminMessages } from '@/messages/admin';
import { common as commonMessages } from '@/messages/common';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { EmptyState, FormAction, FormRow, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { distinct, filterAudit, NO_FILTER, type AuditFilter } from './audit-filter';
import { usePrincipalNames } from './principal-names';

const T = adminMessages.audit;
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
    <Section title={T.title} description={T.loadedNote(loaded.length)}>
      <FormRow className="mb-3">
        <div className="w-48">
          <Select
            label={T.actor}
            value={filter.actor}
            options={menu(distinct(loaded, 'actor'), T.allActors)}
            onChange={(event) => {
              set({ actor: event.target.value });
            }}
          />
        </div>
        <div className="w-56">
          <Select
            label={T.action}
            value={filter.action}
            options={menu(distinct(loaded, 'action'), T.allActions)}
            onChange={(event) => {
              set({ action: event.target.value });
            }}
          />
        </div>
        <div className="w-64">
          <Input
            label={T.contains}
            value={filter.text}
            placeholder={T.containsPlaceholder}
            onChange={(event) => {
              set({ text: event.target.value });
            }}
          />
        </div>
        <FormAction>
          <Button
            disabled={Object.values(filter).every((value) => value === '')}
            onClick={() => {
              setFilter(NO_FILTER);
            }}
          >
            {T.clear}
          </Button>
        </FormAction>
      </FormRow>

      <QueryBoundary query={log}>
        {() =>
          shown.length === 0 ? (
            <EmptyState>{loaded.length === 0 ? T.empty : T.noMatch}</EmptyState>
          ) : (
            <Table caption={T.caption}>
              <THead>
                <TR>
                  <TH>{T.columns.seq}</TH>
                  <TH>{T.columns.time}</TH>
                  <TH>{T.columns.actor}</TH>
                  <TH>{T.columns.action}</TH>
                  <TH>{T.columns.target}</TH>
                  <TH>{T.columns.details}</TH>
                  <TH>{T.columns.hash}</TH>
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
            {log.isFetchingNextPage ? commonMessages.loading : T.loadOlder}
          </Button>
        </div>
      )}
    </Section>
  );
}
