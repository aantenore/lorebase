import { useNavigate } from '@tanstack/react-router';
import { useSchema } from '@/api/queries';
import type { Schema } from '@/api/types';
import { useScope, useScopeSearch } from '@/app/scope';
import { PlannedScreens } from '@/features/planned-screens';
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

function kindName(schema: Schema, id: string): string {
  return schema.kinds.find((k) => k.id === id)?.api_name ?? id;
}

function yesNo(value: boolean) {
  return value ? 'Yes' : 'No';
}

function KindsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption="Kinds">
      <THead>
        <TR>
          <TH>Kind</TH>
          <TH>Primary key</TH>
          <TH>Title Attribute</TH>
          <TH>Attributes</TH>
          <TH>Traits</TH>
          <TH>Status</TH>
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
                .map((t) => schema.traits.find((trait) => trait.id === t.trait_id)?.api_name)
                .join(', ') || '-'}
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
    <Table caption="Attributes">
      <THead>
        <TR>
          <TH>Attribute</TH>
          <TH>Type</TH>
          <TH>Required</TH>
          <TH>Unique</TH>
          <TH>Indexed</TH>
          <TH>Sensitivity</TH>
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
                    <Badge tone="accent">derived</Badge>
                  </span>
                )}
              </TD>
              <TD>{attribute.type}</TD>
              <TD>{yesNo(attribute.required)}</TD>
              <TD>{yesNo(attribute.unique)}</TD>
              <TD>{yesNo(attribute.indexed)}</TD>
              <TD>{attribute.sensitivity.join(', ') || '-'}</TD>
            </TR>
          )),
        )}
      </TBody>
    </Table>
  );
}

function RelationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption="Relations">
      <THead>
        <TR>
          <TH>Relation</TH>
          <TH>Inverse</TH>
          <TH>From</TH>
          <TH>To</TH>
          <TH>Cardinality</TH>
          <TH>Link Attributes</TH>
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
                .join(', ') || '-'}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function TraitsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption="Traits">
      <THead>
        <TR>
          <TH>Trait</TH>
          <TH>Attributes</TH>
          <TH>Implemented by</TH>
        </TR>
      </THead>
      <TBody>
        {schema.traits.map((trait) => (
          <TR key={trait.id}>
            <TD className="font-medium">{trait.api_name}</TD>
            <TD>{trait.attributes.map((a) => `${a.name}: ${a.type}`).join(', ')}</TD>
            <TD>
              {schema.kinds
                .filter((k) => k.traits.some((t) => t.trait_id === trait.id))
                .map((k) => k.api_name)
                .join(', ') || '-'}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function OperationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption="Operations">
      <THead>
        <TR>
          <TH>Operation</TH>
          <TH>Parameters</TH>
          <TH>Review policy</TH>
          <TH>Origin</TH>
          <TH>Status</TH>
        </TR>
      </THead>
      <TBody>
        {schema.operations.map((operation) => (
          <TR key={operation.id}>
            <TD>
              <div className="font-medium">{operation.api_name}</div>
              <div className="text-xs text-muted">{operation.description}</div>
            </TD>
            <TD>{operation.parameters.map((p) => p.name).join(', ') || '-'}</TD>
            <TD>
              <code className="font-mono text-xs">{operation.review_policy}</code>
            </TD>
            <TD>{operation.builtin ? 'Built-in' : 'Custom'}</TD>
            <TD>{operation.status}</TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function DerivationsTable({ schema }: { schema: Schema }) {
  return (
    <Table caption="Derivations">
      <THead>
        <TR>
          <TH>Derivation</TH>
          <TH>Variant</TH>
          <TH>Target</TH>
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
    <Table caption="Sources">
      <THead>
        <TR>
          <TH>Source</TH>
          <TH>Type</TH>
          <TH>Target Kind</TH>
          <TH>Sync</TH>
          <TH>Schedule</TH>
        </TR>
      </THead>
      <TBody>
        {schema.sources.map((source) => (
          <TR key={source.id}>
            <TD className="font-medium">{source.api_name}</TD>
            <TD>{source.type}</TD>
            <TD>{kindName(schema, source.target_kind)}</TD>
            <TD>{source.sync_mode}</TD>
            <TD>{source.schedule ?? 'manual'}</TD>
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

const LABEL_OF: Record<ModelerTab, string> = {
  kinds: 'Kinds',
  attributes: 'Attributes',
  relations: 'Relations',
  traits: 'Traits',
  operations: 'Operations',
  derivations: 'Derivations',
  sources: 'Sources',
};

function TabContent({ tab, schema }: { tab: ModelerTab; schema: Schema }) {
  if (COUNT_OF[tab](schema) === 0)
    return <EmptyState>No {LABEL_OF[tab]} on this Branch.</EmptyState>;
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
      label="Schema elements"
      value={tab}
      onChange={(id) => {
        void navigate({
          to: '.',
          search: { ...scopeSearch, tab: isModelerTab(id) ? id : 'kinds' },
        });
      }}
      tabs={MODELER_TABS.map((id) => ({
        id,
        label: `${LABEL_OF[id]} (${COUNT_OF[id](schema)})`,
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

  return (
    <>
      <PageHeader
        title="Modeler"
        description={`Schema of ${scope.domainInfo.display_name}, edited on a Branch.`}
        actions={
          <div className="flex gap-2">
            <Badge tone="accent">Branch {scope.branch}</Badge>
            {scope.branchInfo.protected && <Badge tone="warning">protected</Badge>}
          </div>
        }
      />
      {scope.branchInfo.protected && (
        <p className="mb-4 rounded-md border border-line bg-surface p-3 text-sm text-muted">
          {scope.branch} is protected. Schema changes go through another Branch and a Proposal.
        </p>
      )}
      <QueryBoundary query={schema}>
        {(data) => (
          <Tabs
            label="Modeler views"
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
                label: 'Overview',
                content: <SchemaOverview tab={tab} schema={data} />,
              },
              {
                id: 'edit',
                label: 'Edit schema',
                content: <SchemaEditor key={data.revision} base={data} />,
              },
            ]}
          />
        )}
      </QueryBoundary>
      <div className="mt-6">
        <PlannedScreens
          area="Modeler"
          built={[
            'Schema overview',
            'Kind editor',
            'Relation editor',
            'Trait editor',
            'Change set summary',
          ]}
        />
      </div>
    </>
  );
}
