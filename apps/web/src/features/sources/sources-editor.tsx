import { lazy, Suspense, useMemo, useState } from 'react';
import { stringify } from 'yaml';
import { diffSchemas } from '@/api/schema-changes';
import { validateSchema, type SchemaIssue } from '@/api/schema-rules';
import type { Schema } from '@/api/types';
import { useScope } from '@/app/scope';
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
          message: `Only the ${SECTION}: section is edited here, as a mapping of Source names.`,
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
  const scope = useScope();
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
  if (section.syntax.length > 0) blocked = 'Fix the YAML errors first.';
  else if (issues.length > 0) blocked = 'Fix the validation issues first.';

  const goTo = (position: Position) => {
    setJump((previous) => ({ ...position, nonce: (previous?.nonce ?? 0) + 1 }));
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted">
          The mapping editor is planned; until then Sources are edited as YAML. Connections are
          secret references, never literals.
        </p>
        <Button
          disabled={text === initial}
          onClick={() => {
            setText(initial);
          }}
        >
          Reset to {scope.branch}
        </Button>
      </div>
      <Suspense
        fallback={
          <p role="status" className="text-muted">
            Loading editor...
          </p>
        }
      >
        <YamlEditor value={text} onChange={setText} label="Sources YAML" jump={jump} />
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
        defaultMessage="Change the Sources"
      />
    </div>
  );
}
