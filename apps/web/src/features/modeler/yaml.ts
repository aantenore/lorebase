import { isNode, LineCounter, parseDocument, stringify } from 'yaml';
import type { SchemaIssue } from '@/api/schema-rules';
import type { SchemaDocument } from './document';

export interface Position {
  line: number;
  col: number;
}

export type SyntaxIssue = SchemaIssue & Position;

export type ParsedYaml = { ok: true; document: unknown } | { ok: false; issues: SyntaxIssue[] };

/** Read schema YAML. An empty text is an empty document; syntax errors carry line and column. */
export function parseSchemaYaml(text: string): ParsedYaml {
  const lineCounter = new LineCounter();
  const document = parseDocument(text, { lineCounter });
  if (document.errors.length > 0) {
    return {
      ok: false,
      issues: document.errors.map((error) => ({
        path: '',
        code: 'yaml_syntax',
        message: error.message.split('\n')[0] ?? error.message,
        line: error.linePos?.[0].line ?? 1,
        col: error.linePos?.[0].col ?? 1,
      })),
    };
  }
  return { ok: true, document: document.toJS() as unknown };
}

/** Write a schema document as YAML, keys in the order of the document. */
export function dumpSchemaYaml(document: SchemaDocument): string {
  return stringify(document, { indent: 2, lineWidth: 0 });
}

/**
 * Where a document path (`kinds/Supplier/attributes/tier/type`) sits in the text. A path
 * that does not exist resolves to its deepest existing parent.
 */
export function locateIssue(text: string, path: string): Position | null {
  const lineCounter = new LineCounter();
  const document = parseDocument(text, { lineCounter });
  const parts = path === '' ? [] : path.split('/');
  for (let length = parts.length; length >= 0; length -= 1) {
    const node: unknown = document.getIn(parts.slice(0, length), true);
    if (isNode(node) && node.range !== undefined && node.range !== null) {
      return lineCounter.linePos(node.range[0]);
    }
  }
  return null;
}
