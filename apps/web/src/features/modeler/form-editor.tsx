import { useState, type ReactNode } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import { modeler as modelerMessages } from '@/messages/modeler';
import { Tabs } from '@/ui/tabs';
import { DerivationsForm } from './derivation-form';
import type { SchemaDocument } from './document';
import { hasIssuesUnder, type Edit, type ElementIds } from './form-helpers';
import { KindsForm } from './kind-form';
import { OperationsForm } from './operation-form';
import { RelationsForm } from './relation-form';
import { SourcesForm } from './source-form';
import { TraitsForm } from './trait-form';

type FormTab = 'kinds' | 'relations' | 'traits' | 'operations' | 'derivations' | 'sources';

const LABEL: Record<FormTab, string> = modelerMessages.form.tabs;

export interface FormEditorProps {
  document: SchemaDocument;
  /** Local issues and the ones Keeper reported on the last dry run, with document paths. */
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  /** The YAML has syntax errors: the form shows the last valid document and cannot edit it. */
  locked: boolean;
  /** Whether an Operation is saved on the Branch exactly as shown, so it can be dry run. */
  savedOperation?: (name: string) => boolean;
  /** Whether a Derivation is saved on the Branch exactly as shown (previewed as Records). */
  savedDerivation?: (name: string) => boolean;
}

/** Form view of the schema document: one editor per element type. */
export function FormEditor({
  document,
  issues,
  edit,
  ids,
  locked,
  savedOperation,
  savedDerivation,
}: FormEditorProps) {
  const [tab, setTab] = useState<FormTab>('kinds');
  const [selected, setSelected] = useState<Partial<Record<FormTab, string>>>({});
  const badge = (prefix: string) =>
    hasIssuesUnder(issues, prefix) ? modelerMessages.form.issueMark : '';
  const props = (key: FormTab) => ({
    document,
    issues,
    edit,
    ids,
    selected: selected[key],
    onSelect: (name: string | undefined) => {
      setSelected((previous) => ({ ...previous, [key]: name }));
    },
  });

  const content: Record<FormTab, ReactNode> = {
    kinds: <KindsForm {...props('kinds')} />,
    relations: <RelationsForm {...props('relations')} />,
    traits: <TraitsForm {...props('traits')} />,
    operations: (
      <OperationsForm
        {...props('operations')}
        {...(savedOperation === undefined ? {} : { saved: savedOperation })}
      />
    ),
    derivations: (
      <DerivationsForm
        {...props('derivations')}
        {...(savedDerivation === undefined ? {} : { saved: savedDerivation })}
      />
    ),
    sources: <SourcesForm {...props('sources')} />,
  };

  return (
    <fieldset disabled={locked} className="min-w-0">
      <legend className="sr-only">{modelerMessages.form.legend}</legend>
      {locked && (
        <p role="status" className="mb-3 rounded-md border border-warning p-2 text-sm text-warning">
          {modelerMessages.form.locked}
        </p>
      )}
      <Tabs
        label={modelerMessages.form.tabsLabel}
        value={tab}
        onChange={(id) => {
          setTab(id as FormTab);
        }}
        tabs={(Object.keys(LABEL) as FormTab[]).map((id) => ({
          id,
          label: `${LABEL[id]}${badge(id)}`,
          content: content[id],
        }))}
      />
    </fieldset>
  );
}
