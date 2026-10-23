import { lazy, Suspense, useMemo, useState } from 'react';
import { stringify } from 'yaml';
import { diffSchemas } from '@/api/schema-changes';
import { validateSchema, type SchemaIssue } from '@/api/schema-rules';
import type { Schema } from '@/api/types';
import {
  documentToSchema,
  schemaToDocument,
  type SchemaDocument,
} from '@/features/modeler/document';
import { memoizedIds } from '@/features/modeler/editor-state';
import { ChangeSetPanel } from '@/features/modeler/change-set-panel';
import { IssuesPanel } from '@/features/modeler/issues-panel';
import { useChangeCheck } from '@/features/modeler/use-change-check';
import { parseSchemaYaml, type Position } from '@/features/modeler/yaml';
import type { Jump } from '@/features/modeler/yaml-editor';
import { sources as sourcesMessages } from '@/messages/sources';
import { Button } from '@/ui/button';

const YamlEditor = lazy(() => import('@/features/modeler/yaml-editor'));

const SECTION = 'sources';

function sectionText(schema: Schema): string {
  return stringify({ [SECTION]: schemaToDocument(schema).sources ?? {} }, { lineWidth: 0 });
}

/** The `sources` mapping the text holds, or why it is not one. */
function readSection(text: string) {
  const parsed = parseSchemaYaml(text);
  if (!parsed.ok) return { syntax: parsed.issues, sources: undefined };
  const root = parsed.document;
  const mapping = (value: unknown): value is Record<string, unknown> =>
    typeof value === 'object' && value !== null && !Array.isArray(value);
  const section = mapping(root) ? root[SECTION] : undefined;
  if (!mapping(root) || Object.keys(root).some((k) => k !== SECTION) || !mapping(section ?? {})) {
    return {
      syntax: [
        {
          path: '',
          code: 'yaml_section',
          message: sourcesMessages.editor.onlySection(SECTION),
          line: 1,
          col: 1,
        },
      ],
      sources: undefined,
    };
  }
  return { syntax: [], sources: section ?? {} };
}

/**
 * Source editor for now: the `sources` section of the schema as YAML, validated with the
 * schema rules and saved as a schema change set (same path as the Modeler editor).
 */
export function SourcesEditor({ base }: { base: Schema }) {
  const [ids] = useState(memoizedIds);
  const initial = useMemo(() => sectionText(base), [base]);
  const [text, setText] = useState(initial);
  const [jump, setJump] = useState<Jump | null>(null);

  const section = useMemo(() => readSection(text), [text]);
  const document = useMemo<SchemaDocument>(
    () => ({
      ...schemaToDocument(base),
      sources: (section.sources ?? {}) as SchemaDocument['sources'],
    }),
    [base, section],
  );
  const converted = useMemo(() => documentToSchema(document, base, ids), [document, base, ids]);
  const issues = useMemo<SchemaIssue[]>(() => {
    const reported = new Set(converted.issues.map((i) => i.path));
    return [
      ...converted.issues,
      ...validateSchema(converted.schema).filter((i) => !reported.has(i.path)),
    ];
  }, [converted]);
  const changes = useMemo(
    () => (section.sources === undefined ? [] : diffSchemas(base, converted.schema)),
    [base, converted, section],
  );

  const valid = section.syntax.length === 0 && issues.length === 0;
  const { check, remote, blocked: checkBlocked } = useChangeCheck(changes, document, valid);
  let blocked = checkBlocked;
  if (section.syntax.length > 0) blocked = sourcesMessages.editor.fixYaml;
  else if (issues.length > 0) blocked = sourcesMessages.editor.fixIssues;

  const goTo = (position: Position) => {
    setJump((previous) => ({ ...position, nonce: (previous?.nonce ?? 0) + 1 }));
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">{sourcesMessages.editor.hint}</p>
        <Button
          disabled={text === initial}
          onClick={() => {
            setText(initial);
          }}
        >
          {sourcesMessages.editor.reset}
        </Button>
      </div>
      <Suspense
        fallback={
          <p role="status" className="text-muted">
            {sourcesMessages.editor.loading}
          </p>
        }
      >
        <YamlEditor
          value={text}
          onChange={setText}
          label={sourcesMessages.editor.yamlLabel}
          jump={jump}
        />
      </Suspense>
      <IssuesPanel
        text={text}
        syntaxIssues={section.syntax}
        issues={issues}
        remote={remote}
        onJump={goTo}
      />
      <ChangeSetPanel
        base={base}
        next={converted.schema}
        changes={changes}
        document={document}
        blocked={blocked}
        check={{ pending: check.isFetching, result: check.data }}
        defaultMessage={sourcesMessages.editor.defaultMessage}
      />
    </div>
  );
}
