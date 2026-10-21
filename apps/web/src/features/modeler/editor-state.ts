import { useCallback, useMemo, useState } from 'react';
import { diffSchemas } from '@/api/schema-changes';
import { validateSchema, type SchemaIssue } from '@/api/schema-rules';
import type { Schema, SchemaChange } from '@/api/types';
import {
  documentToSchema,
  schemaToDocument,
  type IdFactory,
  type SchemaDocument,
} from './document';
import type { Edit } from './form-helpers';
import { dumpSchemaYaml, parseSchemaYaml, type SyntaxIssue } from './yaml';

const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

/**
 * A new element id: the prefix and a 26 character ULID from a secure random source. The first
 * character is 0 to 7, as Keeper requires (a ULID timestamp has 48 bits, which leaves 3 in the
 * first of the 26 base 32 digits).
 */
export function mintId(prefix: string): string {
  const bytes = crypto.getRandomValues(new Uint8Array(26));
  const digits = Array.from(bytes, (b, index) => ALPHABET[b % (index === 0 ? 8 : ALPHABET.length)]);
  return `${prefix}_${digits.join('')}`;
}

/** Ids that stay the same for the same element while it is being edited. */
function memoizedIds(): IdFactory {
  const known = new Map<string, string>();
  return (prefix, key) => {
    const slot = `${prefix}:${key}`;
    const found = known.get(slot);
    if (found !== undefined) return found;
    const id = mintId(prefix);
    known.set(slot, id);
    return id;
  };
}

export interface SchemaEditor {
  /** The YAML text, as the YAML view shows it. */
  text: string;
  /** The last document that parsed; the form edits this one. */
  document: SchemaDocument;
  /** Syntax errors of the current text; empty when it parses. */
  syntaxIssues: SyntaxIssue[];
  /** The schema the document describes. */
  schema: Schema;
  /** Problems of the document, with document paths: conversion first, then schema rules. */
  issues: SchemaIssue[];
  /** Commands that lead from the Branch schema to the edited one. */
  changes: SchemaChange[];
  ids: IdFactory;
  setText: (text: string) => void;
  edit: Edit;
  reset: () => void;
}

/**
 * One document, two views. The YAML text and the form edit the same document: typing
 * valid YAML replaces it, a form edit rewrites the text. While the text does not parse the
 * previous document is kept and the form is locked.
 */
export function useSchemaEditor(base: Schema): SchemaEditor {
  const initial = useMemo(() => schemaToDocument(base), [base]);
  const [document, setDocument] = useState<SchemaDocument>(initial);
  const [text, setTextState] = useState(() => dumpSchemaYaml(initial));
  const [syntaxIssues, setSyntaxIssues] = useState<SyntaxIssue[]>([]);
  const [ids] = useState<IdFactory>(memoizedIds);

  const setText = useCallback((next: string) => {
    setTextState(next);
    const parsed = parseSchemaYaml(next);
    if (parsed.ok) {
      setSyntaxIssues([]);
      setDocument(parsed.document ?? {});
    } else {
      setSyntaxIssues(parsed.issues);
    }
  }, []);

  const edit = useCallback<Edit>(
    (change) => {
      const draft = structuredClone(document);
      change(draft);
      setDocument(draft);
      setTextState(dumpSchemaYaml(draft));
      setSyntaxIssues([]);
    },
    [document],
  );

  const reset = useCallback(() => {
    setDocument(initial);
    setTextState(dumpSchemaYaml(initial));
    setSyntaxIssues([]);
  }, [initial]);

  const converted = useMemo(() => documentToSchema(document, base, ids), [document, base, ids]);
  const issues = useMemo(() => {
    const reported = new Set(converted.issues.map((i) => i.path));
    const rules = validateSchema(converted.schema).filter((i) => !reported.has(i.path));
    return [...converted.issues, ...rules];
  }, [converted]);
  const changes = useMemo(() => diffSchemas(base, converted.schema), [base, converted]);

  return {
    text,
    document,
    syntaxIssues,
    schema: converted.schema,
    issues,
    changes,
    ids,
    setText,
    edit,
    reset,
  };
}
