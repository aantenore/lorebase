import { useNavigate } from '@tanstack/react-router';
import { useSchema } from '@/api/queries';
import type { Schema } from '@/api/types';
import { useScope, useScopeSearch } from '@/app/scope';
import { StartFromData } from '@/features/inference/start-from-data';
import { modeler as modelerMessages } from '@/messages/modeler';
import { Badge, EmptyState, PageHeader, QueryBoundary } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';
import { SchemaEditor } from './schema-editor';
import {
  isModelerTab,
  isModelerView,
  MODELER_TABS,
  type ModelerTab,
  type ModelerView,
} from './search';

const t = modelerMessages.overview;

function kindName(schema: Schema, id: string): string {
  return schema.kinds.find((k) => k.id === id)?.api_name ?? id;
}

function yesNo(value: boolean) {
  return value ? t.yes : t.no;
}

function KindsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.kinds}>
      <THead>
        <TR>
          <TH>{t.columns.kind}</TH>
          <TH>{t.columns.primaryKey}</TH>
          <TH>{t.columns.titleAttribute}</TH>
          <TH>{t.columns.attributes}</TH>
          <TH>{t.columns.traits}</TH>
          <TH>{t.columns.status}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.kinds.map((kind) => (
          <TR key={kind.id}>
            <TD>
              <div className="font-medium">{kind.api_name}</div>
              <div className="text-xs text-muted">{kind.description}</div>
            </TD>
            <TD>{kind.primary_key.map((id) => kind.attributes[id]?.api_name ?? id).join(', ')}</TD>
            <TD>{kind.attributes[kind.title_attribute]?.api_name}</TD>
            <TD>{Object.keys(kind.attributes).length}</TD>
            <TD>
              {kind.traits
                .map((trait) => schema.traits.find((s) => s.id === trait.trait_id)?.api_name)
                .join(', ') || t.blank}
            </TD>
            <TD>
              <Badge tone={kind.status === 'active' ? 'success' : 'neutral'}>{kind.status}</Badge>
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function AttributesTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.attributes}>
      <THead>
        <TR>
          <TH>{t.columns.attribute}</TH>
          <TH>{t.columns.type}</TH>
          <TH>{t.columns.required}</TH>
          <TH>{t.columns.unique}</TH>
          <TH>{t.columns.indexed}</TH>
          <TH>{t.columns.sensitivity}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.kinds.flatMap((kind) =>
          Object.values(kind.attributes).map((attribute) => (
            <TR key={attribute.id}>
              <TD>
                {kind.api_name}.{attribute.api_name}
                {attribute.derivation_id !== null && (
                  <span className="ml-2">
                    <Badge tone="accent">{t.derived}</Badge>
                  </span>
                )}
              </TD>
              <TD>{attribute.type}</TD>
              <TD>{yesNo(attribute.required)}</TD>
              <TD>{yesNo(attribute.unique)}</TD>
              <TD>{yesNo(attribute.indexed)}</TD>
              <TD>{attribute.sensitivity.join(', ') || t.blank}</TD>
            </TR>
          )),
        )}
      </TBody>
    </Table>
  );
}

function RelationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.relations}>
      <THead>
        <TR>
          <TH>{t.columns.relation}</TH>
          <TH>{t.columns.inverse}</TH>
          <TH>{t.columns.from}</TH>
          <TH>{t.columns.to}</TH>
          <TH>{t.columns.cardinality}</TH>
          <TH>{t.columns.linkAttributes}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.relations.map((relation) => (
          <TR key={relation.id}>
            <TD className="font-medium">{relation.api_name}</TD>
            <TD>{relation.inverse_api_name}</TD>
            <TD>{kindName(schema, relation.from_kind)}</TD>
            <TD>{kindName(schema, relation.to_kind)}</TD>
            <TD>{relation.cardinality}</TD>
            <TD>
              {Object.values(relation.attributes)
                .map((a) => a.api_name)
                .join(', ') || t.blank}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function TraitsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.traits}>
      <THead>
        <TR>
          <TH>{t.columns.trait}</TH>
          <TH>{t.columns.attributes}</TH>
          <TH>{t.columns.implementedBy}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.traits.map((trait) => (
          <TR key={trait.id}>
            <TD className="font-medium">{trait.api_name}</TD>
            <TD>{trait.attributes.map((a) => `${a.name}: ${a.type}`).join(', ')}</TD>
            <TD>
              {schema.kinds
                .filter((k) => k.traits.some((x) => x.trait_id === trait.id))
                .map((k) => k.api_name)
                .join(', ') || t.blank}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function OperationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.operations}>
      <THead>
        <TR>
          <TH>{t.columns.operation}</TH>
          <TH>{t.columns.parameters}</TH>
          <TH>{t.columns.reviewPolicy}</TH>
          <TH>{t.columns.origin}</TH>
          <TH>{t.columns.status}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.operations.map((operation) => (
          <TR key={operation.id}>
            <TD>
              <div className="font-medium">{operation.api_name}</div>
              <div className="text-xs text-muted">{operation.description}</div>
            </TD>
            <TD>{operation.parameters.map((p) => p.name).join(', ') || t.blank}</TD>
            <TD>
              <code className="font-mono text-xs">{operation.review_policy}</code>
            </TD>
            <TD>{operation.builtin ? t.builtIn : t.custom}</TD>
            <TD>{operation.status}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function DerivationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.derivations}>
      <THead>
        <TR>
          <TH>{t.columns.derivation}</TH>
          <TH>{t.columns.variant}</TH>
          <TH>{t.columns.target}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.derivations.map((derivation) => (
          <TR key={derivation.id}>
            <TD>
              <div className="font-medium">{derivation.api_name}</div>
              <div className="text-xs text-muted">{derivation.description}</div>
            </TD>
            <TD>{derivation.variant}</TD>
            <TD>
              {kindName(schema, derivation.target.kind_id)}.{derivation.target.api_name}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function SourcesTable({ schema }: { schema: Schema }) {
  return (
    <Table caption={t.tabs.sources}>
      <THead>
        <TR>
          <TH>{t.columns.source}</TH>
          <TH>{t.columns.type}</TH>
          <TH>{t.columns.targetKind}</TH>
          <TH>{t.columns.sync}</TH>
          <TH>{t.columns.schedule}</TH>
        </TR>
      </THead>
      <TBody>
        {schema.sources.map((source) => (
          <TR key={source.id}>
            <TD className="font-medium">{source.api_name}</TD>
            <TD>{source.type}</TD>
            <TD>{kindName(schema, source.target_kind)}</TD>
            <TD>{source.sync_mode}</TD>
            <TD>{source.schedule ?? t.manual}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

const COUNT_OF: Record<ModelerTab, (schema: Schema) => number> = {
  kinds: (s) => s.kinds.length,
  attributes: (s) => s.kinds.reduce((n, k) => n + Object.keys(k.attributes).length, 0),
  relations: (s) => s.relations.length,
  traits: (s) => s.traits.length,
  operations: (s) => s.operations.length,
  derivations: (s) => s.derivations.length,
  sources: (s) => s.sources.length,
};

function TabContent({ tab, schema }: { tab: ModelerTab; schema: Schema }) {
  if (COUNT_OF[tab](schema) === 0) return <EmptyState>{t.empty(t.tabs[tab])}</EmptyState>;
  switch (tab) {
    case 'kinds':
      return <KindsTable schema={schema} />;
    case 'attributes':
      return <AttributesTable schema={schema} />;
    case 'relations':
      return <RelationsTable schema={schema} />;
    case 'traits':
      return <TraitsTable schema={schema} />;
    case 'operations':
      return <OperationsTable schema={schema} />;
    case 'derivations':
      return <DerivationsTable schema={schema} />;
    case 'sources':
      return <SourcesTable schema={schema} />;
  }
}

function SchemaOverview({ tab, schema }: { tab: ModelerTab; schema: Schema }) {
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();
  return (
    <Tabs
      label={t.tabsLabel}
      value={tab}
      onChange={(id) => {
        void navigate({
          to: '.',
          search: { ...scopeSearch, tab: isModelerTab(id) ? id : 'kinds' },
        });
      }}
      tabs={MODELER_TABS.map((id) => ({
        id,
        label: t.tabLabel(t.tabs[id], COUNT_OF[id](schema)),
        content: <TabContent tab={id} schema={schema} />,
      }))}
    />
  );
}

/** Modeler: the schema of the Branch as lists, and an editor with form and YAML views. */
export function ModelerPage({ tab, view }: { tab: ModelerTab; view: ModelerView }) {
  const scope = useScope();
  const schema = useSchema(scope);
  const navigate = useNavigate();
  const scopeSearch = useScopeSearch();
  const m = modelerMessages;

  return (
    <>
      <PageHeader
        title={m.title}
        description={m.description}
        docs={m.docs}
        actions={
          scope.branchInfo.protected ? <Badge tone="warning">{m.protectedBadge}</Badge> : undefined
        }
      />
      {scope.branchInfo.protected && (
        <p className="mb-4 rounded-md border border-line bg-surface p-3 text-sm text-muted">
          {m.protectedNotice}
        </p>
      )}
      <QueryBoundary query={schema}>
        {(data) => (
          <Tabs
            label={m.viewsLabel}
            value={view}
            onChange={(id) => {
              void navigate({
                to: '.',
                search: { ...scopeSearch, tab, view: isModelerView(id) ? id : 'overview' },
              });
            }}
            tabs={[
              {
                id: 'overview',
                label: m.views.overview,
                content: <SchemaOverview tab={tab} schema={data} />,
              },
              {
                id: 'edit',
                label: m.views.edit,
                content: <SchemaEditor key={data.revision} base={data} />,
              },
              {
                id: 'start',
                label: m.views.start,
                content: <StartFromData key={data.revision} base={data} />,
              },
            ]}
          />
        )}
      </QueryBoundary>
    </>
  );
}
