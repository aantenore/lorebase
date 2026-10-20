import { useState } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import { Badge } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { Tabs } from '@/ui/tabs';
import type { SchemaDocument } from './document';
import { hasIssuesUnder, type Edit, type ElementIds } from './form-helpers';
import { KindsForm } from './kind-form';
import { RelationsForm } from './relation-form';
import { TraitsForm } from './trait-form';

type FormTab = 'kinds' | 'relations' | 'traits' | 'operations';

const LABEL: Record<FormTab, string> = {
  kinds: 'Kinds',
  relations: 'Relations',
  traits: 'Traits',
  operations: 'Operations',
};

function YamlOnly({ document, issues }: { document: SchemaDocument; issues: SchemaIssue[] }) {
  const operations = Object.entries(document.operations ?? {});
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        Operations, Derivations and Sources are edited in the YAML view for now. The form lists them
        so a change there is visible here.
      </p>
      {operations.length === 0 ? (
        <p className="text-sm text-muted">No custom Operations.</p>
      ) : (
        <Table caption="Operations in the document">
          <THead>
            <TR>
              <TH>Operation</TH>
              <TH>Parameters</TH>
              <TH>Steps</TH>
              <TH>Status</TH>
            </TR>
          </THead>
          <TBody>
            {operations.map(([name, operation]) => (
              <TR key={name}>
                <TD className="font-medium">
                  {name}{' '}
                  {hasIssuesUnder(issues, `operations/${name}`) && (
                    <Badge tone="danger">issue</Badge>
                  )}
                </TD>
                <TD>
                  {Array.isArray(operation['parameters']) ? operation['parameters'].length : 0}
                </TD>
                <TD>{Array.isArray(operation['steps']) ? operation['steps'].length : 0}</TD>
                <TD>{typeof operation['status'] === 'string' ? operation['status'] : 'active'}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}
      <p className="text-xs text-muted">
        {Object.keys(document.derivations ?? {}).length} Derivations,{' '}
        {Object.keys(document.sources ?? {}).length} Sources.
      </p>
    </div>
  );
}

export interface FormEditorProps {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  /** The YAML has syntax errors: the form shows the last valid document and cannot edit it. */
  locked: boolean;
}

/** Form view of the schema document: Kinds, Relations, Traits, and the Operations list. */
export function FormEditor({ document, issues, edit, ids, locked }: FormEditorProps) {
  const [tab, setTab] = useState<FormTab>('kinds');
  const [kind, setKind] = useState<string | undefined>();
  const [relation, setRelation] = useState<string | undefined>();
  const [trait, setTrait] = useState<string | undefined>();
  const badge = (prefix: string) => (hasIssuesUnder(issues, prefix) ? ' (!)' : '');

  return (
    <fieldset disabled={locked} className="min-w-0">
      <legend className="sr-only">Form editor</legend>
      {locked && (
        <p role="status" className="mb-3 rounded-md border border-warning p-2 text-sm text-warning">
          The YAML has syntax errors. The form shows the last valid version and is read only until
          they are fixed.
        </p>
      )}
      <Tabs
        label="Schema form"
        value={tab}
        onChange={(id) => {
          setTab(id as FormTab);
        }}
        tabs={[
          {
            id: 'kinds',
            label: `${LABEL.kinds}${badge('kinds')}`,
            content: (
              <KindsForm
                document={document}
                issues={issues}
                edit={edit}
                ids={ids}
                selected={kind}
                onSelect={setKind}
              />
            ),
          },
          {
            id: 'relations',
            label: `${LABEL.relations}${badge('relations')}`,
            content: (
              <RelationsForm
                document={document}
                issues={issues}
                edit={edit}
                ids={ids}
                selected={relation}
                onSelect={setRelation}
              />
            ),
          },
          {
            id: 'traits',
            label: `${LABEL.traits}${badge('traits')}`,
            content: (
              <TraitsForm
                document={document}
                issues={issues}
                edit={edit}
                ids={ids}
                selected={trait}
                onSelect={setTrait}
              />
            ),
          },
          {
            id: 'operations',
            label: `${LABEL.operations} (YAML only)`,
            content: <YamlOnly document={document} issues={issues} />,
          },
        ]}
      />
    </fieldset>
  );
}
