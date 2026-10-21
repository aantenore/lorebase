import { Link } from '@tanstack/react-router';
import { useHistory, useLineage, useRecord, useRecordLinks, useSchema } from '@/api/queries';
import type { Kind, Relation, Schema } from '@/api/types';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import {
  Badge,
  EmptyState,
  PageHeader,
  PlaceholderSection,
  QueryBoundary,
  Section,
} from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { ValueView } from './value-view';

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
        <section
          aria-label={`${end.name} links`}
          className="rounded-md border border-line bg-surface p-3"
        >
          <h3 className="text-sm font-medium">
            {end.name} <Badge>{data.links.length}</Badge>
          </h3>
          {data.links.length === 0 ? (
            <p className="mt-1 text-xs text-muted">No linked Records.</p>
          ) : (
            <ul className="mt-2 flex flex-col gap-1">
              {data.links.map((l) => (
                <li key={l.ref}>
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
        </section>
      )}
    </QueryBoundary>
  );
}

function LineageSection({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const lineage = useLineage(scope, kind, recordKey);
  return (
    <Section title="Lineage">
      <QueryBoundary query={lineage}>
        {(data) => (
          <Table caption="Lineage of each value">
            <THead>
              <TR>
                <TH>Attribute</TH>
                <TH>Origin</TH>
                <TH>Actor</TH>
                <TH>At</TH>
                <TH>Revision</TH>
              </TR>
            </THead>
            <TBody>
              {data.lineage.map((entry) => (
                <TR key={entry.attribute}>
                  <TD>{entry.attribute}</TD>
                  <TD>
                    <Badge>{entry.origin_type}</Badge>
                  </TD>
                  <TD>{entry.actor}</TD>
                  <TD>{entry.at}</TD>
                  <TD>
                    <code className="font-mono text-xs">{entry.revision.slice(0, 8)}</code>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </QueryBoundary>
    </Section>
  );
}

function HistorySection({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const history = useHistory(scope, kind, recordKey);
  return (
    <Section title="History">
      <QueryBoundary query={history}>
        {(data) => (
          <ol className="divide-y divide-line rounded-md border border-line bg-surface">
            {data.entries.map((entry) => (
              <li key={entry.revision} className="flex flex-wrap gap-2 px-3 py-2">
                <code className="font-mono text-xs">{entry.revision.slice(0, 8)}</code>
                <span>{entry.message}</span>
                <span className="text-muted">{entry.actor}</span>
                <span className="ml-auto text-xs text-muted">{entry.at}</span>
              </li>
            ))}
          </ol>
        )}
      </QueryBoundary>
    </Section>
  );
}

/** Record page: Attributes, derived values, linked Records, Lineage and history. */
export function RecordPage({ kind, recordKey }: { kind: string; recordKey: string }) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const schema = useSchema(scope);
  const record = useRecord(scope, kind, recordKey);

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
                  description={`${row.ref} on ${scope.branch}`}
                  actions={
                    <Link
                      to="/$domain/canvas"
                      params={{ domain: scope.domain }}
                      search={{ ...scopeSearch, seed: row.ref }}
                      className="text-accent hover:underline"
                    >
                      Open in Canvas
                    </Link>
                  }
                />
                <Section title="Attributes">
                  <Table caption="Attributes">
                    <THead>
                      <TR>
                        <TH>Attribute</TH>
                        <TH>Value</TH>
                        <TH>Type</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {Object.values(definition?.attributes ?? {}).map((attribute) => {
                        const derived = attribute.derivation_id !== null;
                        // Keeper serves a derived value, when it has one, with the stored ones.
                        const value = derived
                          ? (row.derived[attribute.api_name] ?? row.attributes[attribute.api_name])
                          : row.attributes[attribute.api_name];
                        return (
                          <TR key={attribute.id}>
                            <TH scope="row" className="font-normal">
                              {attribute.display_name}
                              {derived && (
                                <span className="ml-2">
                                  <Badge tone="accent">derived</Badge>
                                </span>
                              )}
                            </TH>
                            <TD>
                              <ValueView value={value} type={attribute.type} />
                            </TD>
                            <TD className="text-muted">{attribute.type}</TD>
                          </TR>
                        );
                      })}
                    </TBody>
                  </Table>
                </Section>

                <Section title="Linked Records">
                  {definition === undefined || relationsOf(schemaData, definition).length === 0 ? (
                    <EmptyState>This Kind has no Relations.</EmptyState>
                  ) : (
                    <div className="grid gap-3 md:grid-cols-2">
                      {relationsOf(schemaData, definition).map((end) => (
                        <LinkedGroup
                          key={end.name}
                          domain={scope.domain}
                          kind={kind}
                          recordKey={recordKey}
                          end={end}
                        />
                      ))}
                    </div>
                  )}
                </Section>

                <LineageSection kind={kind} recordKey={recordKey} />
                <HistorySection kind={kind} recordKey={recordKey} />

                <Section title="Run Operation">
                  <PlaceholderSection
                    area="Explorer"
                    screen="Run Operation panel"
                    content="Forms built from Operation parameters, with dry run before commit."
                  />
                </Section>
              </>
            )}
          </QueryBoundary>
        );
      }}
    </QueryBoundary>
  );
}
