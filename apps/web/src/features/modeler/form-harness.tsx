/** Test helpers of the element forms: a document held in state and CodeMirror access. */
import { EditorView } from '@codemirror/view';
import { render, screen } from '@testing-library/react';
import { useState, type ComponentType } from 'react';
import type { SchemaIssue } from '@/api/schema-rules';
import type { SchemaDocument } from './document';
import type { Edit, ElementIds } from './form-helpers';

export interface ElementFormProps {
  document: SchemaDocument;
  issues: SchemaIssue[];
  edit: Edit;
  ids: ElementIds;
  selected: string | undefined;
  onSelect: (name: string | undefined) => void;
}

const ids: ElementIds = (prefix, key) => `${prefix}_${key.replace(/\W/g, '_')}`;

/** Render `Form` over `initial`; `current()` is the document after the edits so far. */
export function renderForm(
  Form: ComponentType<ElementFormProps>,
  initial: SchemaDocument,
  issues: SchemaIssue[] = [],
) {
  let latest = structuredClone(initial);
  function Harness() {
    const [document, setDocument] = useState(latest);
    const [selected, setSelected] = useState<string | undefined>();
    const edit: Edit = (change) => {
      const draft = structuredClone(document);
      change(draft);
      latest = draft;
      setDocument(draft);
    };
    return (
      <Form
        document={document}
        issues={issues}
        edit={edit}
        ids={ids}
        selected={selected}
        onSelect={setSelected}
      />
    );
  }
  const utils = render(<Harness />);
  return { ...utils, current: () => latest };
}

/** The CodeMirror view of the expression field labelled `label`, once it has loaded. */
export async function celView(label: string): Promise<EditorView> {
  const content = await screen.findByRole('textbox', { name: label });
  const host = content.closest<HTMLElement>('.cm-editor');
  const view = host === null ? null : EditorView.findFromDOM(host);
  if (view === null) throw new Error(`No expression editor ${label}.`);
  return view;
}

/** Replace the text of an expression field, as typing would. */
export async function typeCel(label: string, text: string) {
  const view = await celView(label);
  view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
}
