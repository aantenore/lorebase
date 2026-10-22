/**
 * The mock adapter's stand-in for Keeper's previews of unsaved edits. It has no CEL engine:
 * a Derivation shows the values the sample Records hold for its Attribute (what the saved
 * Derivation computed), and an expression mapping may use column references, literals and `+`
 * (Keeper evaluates the full language).
 */
import type {
  DerivationPreview,
  DerivationPreviewRequest,
  SourceExpressionCell,
  SourceExpressionPreview,
  SourceExpressionPreviewRequest,
} from '../preview-types';
import { notFound } from '../errors';
import type { JsonValue } from '../types';
import { runQuery } from './engine';
import type { BranchData } from './state';

type Doc = Record<string, unknown>;

const isDoc = (value: unknown): value is Doc =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const text = (value: unknown) => (typeof value === 'string' ? value : '');

/** Sample Records of the Derivation's Kind with the values they hold for its Attribute. */
export function previewDerivation(
  data: BranchData,
  request: DerivationPreviewRequest,
  limit: number,
): DerivationPreview {
  const derivations = isDoc(request.document) ? request.document['derivations'] : undefined;
  const derivation = isDoc(derivations) ? derivations[request.derivation] : undefined;
  const definition = isDoc(derivation) ? derivation['definition'] : undefined;
  if (!isDoc(definition) || text(definition['kind']) === '') {
    throw notFound(`Derivation ${request.derivation}`);
  }
  const kind = text(definition['kind']);
  const attribute = text(definition['attribute']);
  const page = runQuery(data, { kind, limit: request.limit ?? limit }, limit);
  return {
    revision: data.log[data.log.length - 1]?.id ?? '',
    kind,
    attribute,
    items: page.rows.map((row) => ({
      key: row.key,
      title: row.title,
      value: (row.derived[attribute] ?? row.attributes[attribute] ?? null) as JsonValue,
      masked: false,
      error: null,
    })),
  };
}

const STRING = /^(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")$/;
const ROW_COLUMN = /^row\[\s*(?:'([^']*)'|"([^"]*)")\s*\]$/;

/** Splits on `+` outside string literals. */
function terms(source: string): string[] {
  const parts: string[] = [];
  let current = '';
  let quote = '';
  for (let i = 0; i < source.length; i += 1) {
    const c = source.charAt(i);
    if (quote !== '') {
      current += c;
      if (c === '\\') {
        i += 1;
        current += source.charAt(i);
      } else if (c === quote) quote = '';
    } else if (c === '"' || c === "'") {
      quote = c;
      current += c;
    } else if (c === '+') {
      parts.push(current.trim());
      current = '';
    } else current += c;
  }
  parts.push(current.trim());
  return parts;
}

function term(source: string, row: Record<string, JsonValue>): JsonValue {
  const literal = STRING.exec(source);
  if (literal !== null) return literal[1] ?? literal[2] ?? '';
  if (/^-?\d+(\.\d+)?$/.test(source)) return Number(source);
  const column = ROW_COLUMN.exec(source);
  const name = column === null ? source : (column[1] ?? column[2] ?? '');
  if ((column !== null || /^[A-Za-z_]\w*$/.test(source)) && name !== '') {
    if (name in row) return row[name] ?? null;
    throw new Error(`undeclared reference to '${name}'`);
  }
  throw new Error('The mock evaluates column references, literals and +; Keeper evaluates CEL.');
}

/** The value of `expression` for `row`, within the mock's small subset of CEL. */
export function evaluateRowExpression(expression: string, row: Record<string, JsonValue>) {
  const values = terms(expression).map((part) => term(part, row));
  if (values.length === 1) return values[0] ?? null;
  if (values.every((v) => typeof v === 'number')) {
    return values.reduce<number>((sum, v) => sum + v, 0);
  }
  return values.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join('');
}

export function previewExpressions(
  request: SourceExpressionPreviewRequest,
): SourceExpressionPreview {
  const sources = isDoc(request.document) ? request.document['sources'] : undefined;
  const source = isDoc(sources) ? sources[request.source] : undefined;
  if (!isDoc(source)) throw notFound(`Source ${request.source}`);
  const mapping = isDoc(source['attribute_mapping']) ? source['attribute_mapping'] : {};
  const expressions = Object.entries(mapping).flatMap(([attribute, entry]) =>
    isDoc(entry) && typeof entry['expression'] === 'string'
      ? [[attribute, entry['expression']] as const]
      : [],
  );
  return {
    attributes: expressions.map(([attribute]) => attribute),
    rows: request.rows.map((row) =>
      Object.fromEntries(
        expressions.map(([attribute, expression]): [string, SourceExpressionCell] => {
          try {
            return [attribute, { value: evaluateRowExpression(expression, row), error: null }];
          } catch (error) {
            const message = error instanceof Error ? error.message : 'Cannot be evaluated.';
            return [attribute, { value: null, error: { code: 'expression.type', message } }];
          }
        }),
      ),
    ),
  };
}
