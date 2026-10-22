/**
 * Edits of the transforms of an Attribute mapping of a Source. Each helper changes only the
 * keys it is about: other keys of a transform stay where they are.
 */
import { asList, asText, isRecord } from './form-helpers';

type Doc = Record<string, unknown>;

/** Transform names of the core Source model, in the order it lists them. */
export const TRANSFORM_NAMES = [
  'cast',
  'trim',
  'lower',
  'upper',
  'parse_date',
  'concat',
  'constant',
  'map',
  'null_if',
] as const;
export type TransformName = (typeof TRANSFORM_NAMES)[number];

export const CAST_TARGETS = [
  'string',
  'text',
  'integer',
  'decimal',
  'float',
  'boolean',
  'date',
  'timestamp',
] as const;

const DEFAULT_ARGS: Partial<Record<TransformName, () => Doc>> = {
  cast: () => ({ to: 'string' }),
  parse_date: () => ({ format: '%Y-%m-%d' }),
  concat: () => ({ columns: [] }),
  constant: () => ({ value: '' }),
  map: () => ({ values: {} }),
  null_if: () => ({ values: [] }),
};

/** Switch a transform to `name`: the arguments of the previous name go, the new ones start. */
export function changeTransformName(transform: Doc, name: string) {
  transform['name'] = name;
  const defaults = DEFAULT_ARGS[name as TransformName];
  if (defaults === undefined) delete transform['args'];
  else transform['args'] = defaults();
}

/** The arguments of a transform, created in place when missing. */
export function argsOf(transform: Doc): Doc {
  const args = transform['args'];
  if (isRecord(args)) return args;
  const created: Doc = {};
  transform['args'] = created;
  return created;
}

/** What Scribe would refuse about a transform; it checks again when the Source is read. */
export function transformProblem(transform: Doc): string | undefined {
  const args = isRecord(transform['args']) ? transform['args'] : {};
  switch (transform['name']) {
    case 'cast':
      return CAST_TARGETS.some((t) => t === args['to'])
        ? undefined
        : `cast needs a target type: ${CAST_TARGETS.join(', ')}.`;
    case 'parse_date':
      return typeof args['format'] === 'string' ? undefined : 'parse_date needs a format.';
    case 'map':
      return isRecord(args['values']) ? undefined : 'map needs a table of values.';
    case 'constant':
      return 'value' in args ? undefined : 'constant needs a value.';
    case 'null_if':
      return 'value' in args || 'values' in args ? undefined : 'null_if needs values.';
    case 'concat':
      return Array.isArray(args['columns']) ? undefined : 'concat needs a list of columns.';
    default:
      return TRANSFORM_NAMES.some((n) => n === transform['name'])
        ? undefined
        : `Unknown transform ${asText(transform['name'])}.`;
  }
}

/** The text of a scalar argument: text as it is, other values as JSON. */
export function scalarText(value: unknown): string {
  if (value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

/**
 * The value of a scalar argument typed as `text`. A value that was a number, boolean or null
 * stays one while the text is still that kind of JSON; everything else is text.
 */
export function scalarFrom(text: string, previous: unknown): unknown {
  if (previous === undefined || typeof previous === 'string') return text;
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed === null || typeof parsed === 'number' || typeof parsed === 'boolean'
      ? parsed
      : text;
  } catch {
    return text;
  }
}

/** The values null_if turns into null: `values`, or the single `value`. */
export function nullIfValues(args: Doc): unknown[] {
  if (Array.isArray(args['values'])) return args['values'];
  return 'value' in args ? [args['value']] : [];
}

/** The transforms of a mapping entry as records, in order. */
export function transformsOf(entry: Doc | undefined): Doc[] {
  return asList(entry?.['transforms']).map((t) => (isRecord(t) ? t : {}));
}
