import { Link } from '@tanstack/react-router';
import { useState, type ReactNode } from 'react';
import { useHistory, useLineage, useRecord, useRecordLinks, useSchema } from '@/api/queries';
import type { AttributeLineage, Kind, RecordChange, Relation, Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { record as recordMessages } from '@/messages/record';
import { Button } from '@/ui/button';
import {
  Badge,
  type BadgeTone,
  Card,
  CardGrid,
  EmptyState,
  PageHeader,
  QueryBoundary,
  Section,
} from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';
import { shortTime } from '@/ui/time';
import { CandidateMark, useRecordMarks } from '@/features/reconcile/record-marks';
import { ValueView } from './value-view';

const t = recordMessages;

interface RelationEnd {
  /** Name to request links with: forward or inverse api name. */
  name: string;
  relation: Relation;
}

function relationsOf(schema: Schema, kind: Kind): RelationEnd[] {
  return schema.relations.flatMap((relation) => [
    ...(relation.from_kind === kind.id ? [{ name: relation.api_name, relation }] : []),
    ...(relation.to_kind === kind.id ? [{ name: relation.inverse_api_name, relation }] : []),
  ]);
}

function LinkedGroup({
  domain,
  kind,
  recordKey,
  end,
}: {
  domain: string;
  kind: string;
  recordKey: string;
  end: RelationEnd;
}) {
  const scope = useScope();
  const links = useRecordLinks(scope, kind, recordKey, end.name);
  return (
    <QueryBoundary query={links}>
      {(data) => (
        <Card as="section" aria-label={t.links.groupLabel(end.name)}>
          <h3 className="text-sm font-medium break-words">
            {end.name} <Badge>{data.links.length}</Badge>
          </h3>
          {data.links.length === 0 ? (
            <p className="mt-1 text-xs text-muted">{t.links.none}</p>
          ) : (
            <ul className="mt-2 flex flex-col gap-1">
              {data.links.map((l) => (
                <li key={l.ref} className="min-w-0 break-words">
                  <RecordLink domain={domain} recordRef={l.ref}>
                    {l.title}
                  </RecordLink>
                  {Object.entries(l.attributes).map(([name, value]) => (
                    <span key={name} className="ml-2 text-xs text-muted">
                      {name}: <ValueView value={value} />
                    </span>
                  ))}
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}
    </QueryBoundary>
  );
}

function Dash() {
  return <span className="text-muted">-</span>;
}

function ShortRevision({ id }: { id: string | null | undefined }) {
  if (id === null || id === undefined || id === '') return <Dash />;
  return <code className="font-mono text-xs">{id.slice(0, 8)}</code>;
}

/** Source, Operation, extraction evidence and Proposal behind one value. */
function LineageDetail({ entry }: { entry: AttributeLineage }) {
  const parts: ReactNode[] = [];
  if (entry.source != null) parts.push(<span key="source">{t.lineage.source(entry.source)}</span>);
  if (entry.operation != null)
    parts.push(<span key="operation">{t.lineage.operation(entry.operation)}</span>);
  if (entry.extraction != null) {
    const e = entry.extraction;
    parts.push(
      <span key="extraction">
        {t.lineage.extraction({
          jobId: e.job_id,
          documentId: e.document_id,
          start: e.span_start,
          end: e.span_end,
          model: e.model_id,
          prompt: e.prompt_version,
        })}
      </span>,
    );
  }
  if (entry.proposal != null) {
    parts.push(
      <span key="proposal">
        {t.lineage.proposal(entry.proposal.title)} <Badge>{entry.proposal.status}</Badge>
      </span>,
    );
  }
  if (parts.length === 0) return <Dash />;
  return <div className="flex min-w-48 flex-col gap-0.5 text-xs break-words">{parts}</div>;
}

function LineageSection({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const lineage = useLineage(scope, kind, recordKey);
  return (
    <Section title={t.lineage.title}>
      <QueryBoundary query={lineage}>
        {(data) => (
          <Table caption={t.lineage.caption}>
            <THead>
              <TR>
                <TH>{t.lineage.columnAttribute}</TH>
                <TH>{t.lineage.columnOrigin}</TH>
                <TH>{t.lineage.columnValue}</TH>
                <TH>{t.lineage.columnDetail}</TH>
                <TH>{t.lineage.columnRevision}</TH>
                <TH>{t.lineage.columnAuthor}</TH>
                <TH>{t.lineage.columnAt}</TH>
              </TR>
            </THead>
            <TBody>
              {data.attributes.map((entry) => (
                <TR key={entry.name}>
                  <TD>{entry.name}</TD>
                  <TD>
                    {entry.provenance == null ? (
                      <Dash />
                    ) : (
                      <Badge>{entry.provenance.origin_type}</Badge>
                    )}
                  </TD>
                  <TD>
                    <ValueView value={entry.value} />
                  </TD>
                  <TD>
                    <LineageDetail entry={entry} />
                  </TD>
                  <TD>
                    <ShortRevision id={entry.revision} />
                  </TD>
                  <TD>{entry.author ?? entry.provenance?.actor ?? <Dash />}</TD>
                  <TD>{shortTime(entry.revision_at ?? entry.provenance?.at)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </QueryBoundary>
    </Section>
  );
}

const CHANGE_TONE: Record<RecordChange['change'], BadgeTone> = {
  created: 'success',
  updated: 'accent',
  deleted: 'danger',
};

function ChangeItem({ change }: { change: RecordChange }) {
  return (
    <li className="px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={CHANGE_TONE[change.change]}>{change.change}</Badge>
        <ShortRevision id={change.revision} />
        <span className="min-w-0 break-words">{change.message}</span>
        <span className="text-muted">{change.author}</span>
        <time dateTime={change.at} className="ml-auto text-xs text-muted">
          {shortTime(change.at)}
        </time>
      </div>
      {change.attributes.length > 0 && (
        <ul
          aria-label={t.history.changedAttributes(change.revision.slice(0, 8))}
          className="mt-1 text-xs"
        >
          {change.attributes.map((attribute) => (
            <li key={attribute.name}>
              <span className="font-medium">{attribute.name}</span>:{' '}
              <ValueView value={attribute.before} />
              <span aria-label={t.history.changedTo}> {'\u2192'} </span>
              <ValueView value={attribute.after} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function HistoryTab({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const history = useHistory(scope, kind, recordKey);
  return (
    <Section title={t.history.title}>
      <QueryBoundary query={history}>
        {(data) => {
          const changes = data.pages.flatMap((page) => page.items);
          if (changes.length === 0) return <EmptyState>{t.history.empty}</EmptyState>;
          return (
            <>
              <ol
                aria-label={t.history.listLabel}
                className="divide-y divide-line rounded-md border border-line bg-surface"
              >
                {changes.map((change) => (
                  <ChangeItem key={`${change.revision}-${change.generation}`} change={change} />
                ))}
              </ol>
              {history.hasNextPage && (
                <div className="mt-3">
                  <Button
                    disabled={history.isFetchingNextPage}
                    onClick={() => {
                      void history.fetchNextPage();
                    }}
                  >
                    {t.history.loadMore}
                  </Button>
                </div>
              )}
            </>
          );
        }}
      </QueryBoundary>
    </Section>
  );
}

/** Record page: Attributes, derived values, linked Records, Lineage and a History tab. */
export function RecordPage({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const schema = useSchema(scope);
  const record = useRecord(scope, kind, recordKey);
  const [tab, setTab] = useState('details');
  const marks = useRecordMarks(kind, recordKey);

  return (
    <QueryBoundary query={schema}>
      {(schemaData) => {
        const definition = schemaData.kinds.find((k) => k.api_name === kind);
        return (
          <QueryBoundary query={record}>
            {({ record: row }) => (
              <>
                <PageHeader
                  title={row.title}
                  description={row.ref}
                  actions={
                    <Link
                      to="/$domain/canvas"
                      params={{ domain: scope.domain }}
                      search={{ ...scopeSearch, seed: row.ref }}
                      className="text-accent hover:underline"
                    >
                      {t.openInCanvas}
                    </Link>
                  }
                />
                <Tabs
                  label={t.tabsLabel}
                  value={tab}
                  onChange={setTab}
                  tabs={[
                    {
                      id: 'details',
                      label: t.tabDetails,
                      content: (
                        <>
                          <Section
                            title={t.attributes.title}
                            {...(marks.duplicates.length === 0
                              ? {}
                              : {
                                  actions: (
                                    <CandidateMark
                                      candidates={marks.duplicates}
                                      label={reconcileMessages.record.duplicate(
                                        marks.duplicates
                                          .flatMap((c) => c.keys)
                                          .filter((k) => k !== recordKey)
                                          .join(', '),
                                      )}
                                    />
                                  ),
                                })}
                          >
                            <Table caption={t.attributes.caption}>
                              <THead>
                                <TR>
                                  <TH>{t.attributes.columnAttribute}</TH>
                                  <TH>{t.attributes.columnValue}</TH>
                                  <TH>{t.attributes.columnType}</TH>
                                </TR>
                              </THead>
                              <TBody>
                                {Object.values(definition?.attributes ?? {}).map((attribute) => {
                                  const derived = attribute.derivation_id !== null;
                                  // Keeper serves a derived value, when it has one, with the stored ones.
                                  const value = derived
                                    ? (row.derived[attribute.api_name] ??
                                      row.attributes[attribute.api_name])
                                    : row.attributes[attribute.api_name];
                                  return (
                                    <TR key={attribute.id}>
                                      <TH scope="row" className="font-normal">
                                        {attribute.display_name}
                                        {derived && (
                                          <span className="ml-2">
                                            <Badge tone="accent">{t.attributes.derived}</Badge>
                                          </span>
                                        )}
                                      </TH>
                                      <TD>
                                        <ValueView value={value} type={attribute.type} />
                                        {marks.byAttribute.has(attribute.api_name) && (
                                          <span className="ml-2">
                                            <CandidateMark
                                              candidates={
                                                marks.byAttribute.get(attribute.api_name) ?? []
                                              }
                                            />
                                          </span>
                                        )}
                                      </TD>
                                      <TD className="text-muted">{attribute.type}</TD>
                                    </TR>
                                  );
                                })}
                              </TBody>
                            </Table>
                          </Section>

                          <Section title={t.links.title}>
                            {definition === undefined ||
                            relationsOf(schemaData, definition).length === 0 ? (
                              <EmptyState>{t.links.noRelations}</EmptyState>
                            ) : (
                              <CardGrid minWidth="20rem">
                                {relationsOf(schemaData, definition).map((end) => (
                                  <LinkedGroup
                                    key={end.name}
                                    domain={scope.domain}
                                    kind={kind}
                                    recordKey={recordKey}
                                    end={end}
                                  />
                                ))}
                              </CardGrid>
                            )}
                          </Section>

                          <LineageSection kind={kind} recordKey={recordKey} />
                        </>
                      ),
                    },
                    {
                      id: 'history',
                      label: t.tabHistory,
                      content: <HistoryTab kind={kind} recordKey={recordKey} />,
                    },
                  ]}
                />
              </>
            )}
          </QueryBoundary>
        );
      }}
    </QueryBoundary>
  );
}
