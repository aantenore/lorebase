import { useCallback, useMemo, useState } from 'react';
import type { SchemaProposalDraft } from '@/api/inference-types';
import { diffSchemas } from '@/api/schema-changes';
import { validateSchema, type SchemaIssue } from '@/api/schema-rules';
import type { Schema, SchemaChange } from '@/api/types';
import {
  documentToSchema,
  schemaToDocument,
  type SchemaDocument,
} from '@/features/modeler/document';
import { memoizedIds } from '@/features/modeler/editor-state';
import type { Edit } from '@/features/modeler/form-helpers';
import { dumpSchemaYaml, parseSchemaYaml } from '@/features/modeler/yaml';
import {
  acceptAtOrAbove,
  analyze,
  countDecisions,
  decide,
  decideKind,
  decideRelation,
  indexEvidence,
  mergeIntoBase,
  sharedNames,
  withIds,
  type Analysis,
  type Decision,
  type Decisions,
  type DecisionCounts,
  type EvidenceIndex,
} from './proposal-model';
import { inference as inferenceMessages } from '@/messages/inference';

export interface ProposalDraft {
  /** The proposal with the reviewer's edits; every element, accepted or not. */
  document: SchemaDocument;
  decisions: Decisions;
  evidence: EvidenceIndex;
  counts: DecisionCounts;
  analysis: Analysis;
  /** YAML of the accepted elements: what the proposal would add. */
  yaml: string;
  /** Names the proposal shares with the schema of the Branch. */
  shared: { kinds: ReadonlySet<string>; relations: ReadonlySet<string> };
  /** The schema of the Branch with the accepted elements laid over it. */
  next: Schema;
  /** Problems of that schema, with document paths. */
  issues: SchemaIssue[];
  /** Commands that lead from the Branch schema to `next`. */
  changes: SchemaChange[];
  /** Why the proposal text could not be read; the tree is empty then. */
  parseError: string | null;
  edit: Edit;
  decideElement: (id: string, value: Decision | null) => void;
  decideKindElement: (name: string, value: Decision | null) => void;
  decideRelationElement: (name: string, value: Decision | null) => void;
  acceptAtOrAbove: (threshold: number) => void;
}

/**
 * State of the review: the proposal document (renames and type changes), the decision per
 * element, and everything derived from them, up to the change set against the Branch schema.
 */
export function useProposalDraft(proposal: SchemaProposalDraft, base: Schema): ProposalDraft {
  const [ids] = useState(memoizedIds);
  const parsed = useMemo(() => {
    const result = parseSchemaYaml(proposal.yaml);
    if (!result.ok) {
      return {
        document: {},
        error: result.issues[0]?.message ?? inferenceMessages.invalidYaml,
      };
    }
    return { document: withIds(result.document ?? {}, ids), error: null };
  }, [proposal.yaml, ids]);

  const [document, setDocument] = useState<SchemaDocument>(parsed.document);
  const [decisions, setDecisions] = useState<Decisions>({});
  const evidence = useMemo(() => indexEvidence(proposal.evidence), [proposal.evidence]);

  const edit = useCallback<Edit>((change) => {
    setDocument((current) => {
      const draft = structuredClone(current);
      change(draft);
      return draft;
    });
  }, []);

  const decideElement = useCallback((id: string, value: Decision | null) => {
    setDecisions((current) => decide(current, id, value));
  }, []);
  const decideKindElement = useCallback(
    (name: string, value: Decision | null) => {
      const kind = document.kinds?.[name];
      if (kind !== undefined) setDecisions((current) => decideKind(current, kind, value));
    },
    [document],
  );
  const decideRelationElement = useCallback(
    (name: string, value: Decision | null) => {
      const relation = document.relations?.[name];
      if (relation !== undefined)
        setDecisions((current) => decideRelation(current, relation, value));
    },
    [document],
  );
  const accept = useCallback(
    (threshold: number) => {
      setDecisions((current) => acceptAtOrAbove(document, evidence, current, threshold));
    },
    [document, evidence],
  );

  const baseDocument = useMemo(() => schemaToDocument(base), [base]);
  const analysis = useMemo(() => analyze(document, decisions), [document, decisions]);
  const converted = useMemo(
    () => documentToSchema(mergeIntoBase(baseDocument, analysis.document), base, ids),
    [baseDocument, analysis, base, ids],
  );
  const issues = useMemo(() => {
    const reported = new Set(converted.issues.map((i) => i.path));
    const rules = validateSchema(converted.schema).filter((i) => !reported.has(i.path));
    return [...converted.issues, ...rules];
  }, [converted]);
  const changes = useMemo(() => diffSchemas(base, converted.schema), [base, converted]);

  return {
    document,
    decisions,
    evidence,
    counts: useMemo(() => countDecisions(document, decisions), [document, decisions]),
    analysis,
    yaml: useMemo(() => dumpSchemaYaml(analysis.document), [analysis]),
    shared: useMemo(() => sharedNames(baseDocument, document), [baseDocument, document]),
    next: converted.schema,
    issues,
    changes,
    parseError: parsed.error,
    edit,
    decideElement,
    decideKindElement,
    decideRelationElement,
    acceptAtOrAbove: accept,
  };
}
