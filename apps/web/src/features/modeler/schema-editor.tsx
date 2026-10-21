import { lazy, Suspense, useState } from 'react';
import type { Schema } from '@/api/types';
import { useScope } from '@/app/scope';
import { Button } from '@/ui/button';
import { Section } from '@/ui/layout';
import { ChangeSetPanel } from './change-set-panel';
import { useSchemaEditor } from './editor-state';
import { FormEditor } from './form-editor';
import { IssuesPanel } from './issues-panel';
import { useChangeCheck } from './use-change-check';
import type { Position } from './yaml';
import type { Jump } from './yaml-editor';

// CodeMirror is the heavy part of the Modeler; it loads when the editor opens.
const YamlEditor = lazy(() => import('./yaml-editor'));

/**
 * Schema editing on a Branch: a form and a YAML editor over one document, validation
 * issues with paths, and the change set the edits add up to.
 */
export function SchemaEditor({ base }: { base: Schema }) {
  const scope = useScope();
  const editor = useSchemaEditor(base);
  const [jump, setJump] = useState<Jump | null>(null);

  const documentIsValid = editor.syntaxIssues.length === 0 && editor.issues.length === 0;
  const hasChanges = editor.changes.length > 0;
  const {
    check,
    remote,
    blocked: checkBlocked,
  } = useChangeCheck(editor.changes, editor.document, documentIsValid && hasChanges);

  let blocked = checkBlocked;
  if (editor.syntaxIssues.length > 0) blocked = 'Fix the YAML syntax errors first.';
  else if (editor.issues.length > 0) blocked = 'Fix the validation issues first.';

  const goTo = (position: Position) => {
    setJump((previous) => ({ ...position, nonce: (previous?.nonce ?? 0) + 1 }));
  };

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          Form and YAML edit the same document. Changes stay local until saved.
        </p>
        <Button disabled={!hasChanges && editor.syntaxIssues.length === 0} onClick={editor.reset}>
          Reset to {scope.branch}
        </Button>
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-2">
        <Section title="Form" className="mb-0 min-w-0">
          <FormEditor
            document={editor.document}
            issues={editor.issues}
            edit={editor.edit}
            ids={editor.ids}
            locked={editor.syntaxIssues.length > 0}
          />
        </Section>
        <Section title="YAML" className="mb-0 min-w-0">
          <Suspense
            fallback={
              <p role="status" className="text-muted">
                Loading editor...
              </p>
            }
          >
            <YamlEditor
              value={editor.text}
              onChange={editor.setText}
              label="Schema YAML"
              jump={jump}
            />
          </Suspense>
        </Section>
      </div>

      <IssuesPanel
        text={editor.text}
        syntaxIssues={editor.syntaxIssues}
        issues={editor.issues}
        remote={remote}
        onJump={goTo}
      />

      <ChangeSetPanel
        base={base}
        next={editor.schema}
        changes={editor.changes}
        document={editor.document}
        blocked={blocked}
        check={{ pending: check.isFetching, result: check.data }}
      />
    </div>
  );
}
