/**
 * Preview of a Source mapping on a sample file: the first rows of the file, read and mapped onto
 * the target Kind the way Scribe maps them (key mapping, column mapping, transforms, type
 * coercion). The file is read in the browser and goes nowhere; Scribe stays authoritative.
 */
import { asList, asText, isRecord } from './form-helpers';

type Doc = Record<string, unknown>;
export type Row = Record<string, unknown>;

export interface Sample {
  rows: Row[];
  /** Why the file could not be read, when it could not. */
  error?: string;
}

export interface MappedCell {
  value?: string | null;
  error?: string;
}

export interface MappedRow {
  key: MappedCell;
  cells: Record<string, MappedCell>;
}

// ------------------------------------------------------------------ reading

/** Rows of delimited text with a header line; quoted fields may hold delimiters and newlines. */
export function parseDelimited(text: string, delimiter: string): Row[] {
  const lines: string[][] = [];
  let field = '';
  let line: string[] = [];
  let quoted = false;
  const endLine = () => {
    line.push(field);
    field = '';
    if (line.length > 1 || line[0] !== '') lines.push(line);
    line = [];
  };
  for (let i = 0; i < text.length; i += 1) {
    const char = text.charAt(i);
    if (quoted) {
      if (char === '"' && text.charAt(i + 1) === '"') {
        field += '"';
        i += 1;
      } else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === delimiter) {
      line.push(field);
      field = '';
    } else if (char === '\n') endLine();
    else if (char !== '\r') field += char;
  }
  endLine();
  const [header, ...body] = lines;
  if (header === undefined) return [];
  return body.map((values) => Object.fromEntries(header.map((name, i) => [name, values[i] ?? ''])));
}

/** `value` with nested objects turned into `parent<separator>child` keys. */
export function flatten(value: Row, separator: string, prefix = ''): Row {
  const out: Row = {};
  for (const [key, item] of Object.entries(value)) {
    const name = prefix === '' ? key : `${prefix}${separator}${key}`;
    if (isRecord(item) && separator !== '') Object.assign(out, flatten(item, separator, name));
    else out[name] = item;
  }
  return out;
}

function atPath(document: unknown, path: string): unknown {
  let node = document;
  for (const part of path === '' ? [] : path.split('.')) {
    if (!isRecord(node)) return undefined;
    node = node[part];
  }
  return node;
}

export interface SampleOptions {
  /** Dotted path of the array of records in a JSON document. */
  recordsPath: string;
  flattenSeparator: string;
  limit: number;
}

/** The first `limit` rows of a CSV, TSV, JSON or JSON Lines file, chosen by the file name. */
export function readSample(fileName: string, text: string, options: SampleOptions): Sample {
  const extension = fileName.toLowerCase().split('.').pop() ?? '';
  const take = (rows: Row[]): Sample => ({
    rows: rows.slice(0, options.limit).map((r) => flatten(r, options.flattenSeparator)),
  });
  try {
    if (extension === 'csv') return take(parseDelimited(text, ','));
    if (extension === 'tsv') return take(parseDelimited(text, '\t'));
    if (extension === 'jsonl' || extension === 'ndjson') {
      const lines = text.split('\n').filter((l) => l.trim() !== '');
      return take(lines.slice(0, options.limit).map((l) => asRow(JSON.parse(l))));
    }
    if (extension === 'json') {
      const found = atPath(JSON.parse(text), options.recordsPath);
      if (!Array.isArray(found)) {
        return {
          rows: [],
          error:
            options.recordsPath === ''
              ? 'The document is not an array of records; set the records path.'
              : `${options.recordsPath} is not an array of records.`,
        };
      }
      return take(found.map(asRow));
    }
  } catch (error) {
    return { rows: [], error: error instanceof Error ? error.message : 'The file cannot be read.' };
  }
  return { rows: [], error: 'The preview reads CSV, TSV, JSON and JSON Lines files.' };
}

function asRow(value: unknown): Row {
  if (!isRecord(value)) throw new Error('A record of the file is not an object.');
  return value;
}

// ------------------------------------------------------------------ mapping

const TRUE = new Set(['true', 't', 'yes', 'y', '1']);
const FALSE = new Set(['false', 'f', 'no', 'n', '0']);

const isEmpty = (value: unknown) =>
  value === null || value === undefined || (typeof value === 'string' && value.trim() === '');

/** The text form of a value, as Scribe uses it for textual Attributes and string transforms. */
export function textOf(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'object' && value !== null) return JSON.stringify(value);
  return String(value);
}

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'];

const pad = (n: number, width = 2) => String(n).padStart(width, '0');

/** A date in `format` (strptime directives %Y %y %m %d %b %B %%) as `YYYY-MM-DD`. */
export function parseDate(text: string, format: string): string {
  const order: string[] = [];
  const pattern = format
    .replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    .replace(/%([YymdbB%])/g, (_, directive: string) => {
      if (directive === '%') return '%';
      order.push(directive);
      if (directive === 'Y') return '(\\d{4})';
      if (directive === 'b' || directive === 'B') return '([A-Za-z]+)';
      return '(\\d{1,2})';
    });
  const match = new RegExp(`^${pattern}$`).exec(text.trim());
  if (match === null) throw new Error(`${text} does not match ${format}`);
  let year = 1900;
  let month = 1;
  let day = 1;
  order.forEach((directive, i) => {
    const part = match[i + 1] ?? '';
    if (directive === 'Y') year = Number(part);
    else if (directive === 'y') year = 2000 + Number(part) - (Number(part) >= 69 ? 100 : 0);
    else if (directive === 'm') month = Number(part);
    else if (directive === 'd') day = Number(part);
    else month = MONTHS.indexOf(part.slice(0, 3).toLowerCase()) + 1;
  });
  const check = new Date(Date.UTC(year, month - 1, day));
  if (month < 1 || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) {
    throw new Error(`${text} is not a date`);
  }
  return `${pad(year, 4)}-${pad(month)}-${pad(day)}`;
}

function castTo(value: unknown, target: string): unknown {
  if (target === 'string' || target === 'text') return textOf(value);
  const text = textOf(value).trim();
  if (target === 'integer') {
    if (!/^[-+]?\d+(\.0*)?$/.test(text)) throw new Error(`${text} is not an integer`);
    return Math.trunc(Number(text));
  }
  if (target === 'decimal') {
    if (!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(text)) throw new Error(`${text} is not a decimal`);
    return text;
  }
  if (target === 'float') {
    if (text === '' || !Number.isFinite(Number(text))) throw new Error(`${text} is not a number`);
    return Number(text);
  }
  if (target === 'boolean') {
    if (typeof value === 'boolean') return value;
    if (TRUE.has(text.toLowerCase())) return true;
    if (FALSE.has(text.toLowerCase())) return false;
    throw new Error(`not a boolean: ${text}`);
  }
  if (target === 'date') {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) throw new Error(`${text} is not an ISO date`);
    return text;
  }
  if (Number.isNaN(Date.parse(text))) throw new Error(`${text} is not an ISO timestamp`);
  return text;
}

/** One transform on a value of `row`, as Scribe applies it. */
export function applyTransform(transform: Doc, value: unknown, row: Row): unknown {
  const args = isRecord(transform['args']) ? transform['args'] : {};
  const name = asText(transform['name']);
  if (name === 'constant') return args['value'];
  if (name === 'concat') {
    const parts = [value, ...asList(args['columns']).map((c) => row[textOf(c)])];
    return parts
      .filter((p) => !isEmpty(p))
      .map(textOf)
      .join(asText(args['separator']));
  }
  if (value === null || value === undefined) return null;
  switch (name) {
    case 'trim':
      return typeof value === 'string' ? value.trim() : value;
    case 'lower':
      return textOf(value).toLowerCase();
    case 'upper':
      return textOf(value).toUpperCase();
    case 'null_if': {
      const candidates = Array.isArray(args['values']) ? args['values'] : [args['value']];
      return candidates.some((c) => textOf(c) === textOf(value)) ? null : value;
    }
    case 'map': {
      const values = isRecord(args['values']) ? args['values'] : {};
      const found = values[textOf(value)];
      return found ?? ('default' in args ? args['default'] : value);
    }
    case 'parse_date':
      return parseDate(textOf(value), asText(args['format']));
    case 'cast':
      return castTo(value, asText(args['to']));
    default:
      throw new Error(`Unknown transform ${name}`);
  }
}

/** The canonical text of a value of an Attribute of `type`, or why it is not one. */
function coerce(value: unknown, type: string): string | null {
  if (isEmpty(value)) return null;
  if (type === 'string' || type === 'text') return textOf(value);
  if (type === 'integer' || type === 'decimal' || type === 'float' || type === 'boolean') {
    return textOf(castTo(value, type));
  }
  if (type === 'date' || type === 'timestamp') return textOf(castTo(value, type));
  return textOf(value);
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : 'Cannot be mapped.';
}

/**
 * Maps `rows` the way the Source maps them. `types` holds the type label of each Attribute of
 * the target Kind that a Source can map (derived Attributes left out).
 */
export function mapRows(rows: Row[], source: Doc, types: Record<string, string>): MappedRow[] {
  const keyColumns = asList(source['key_mapping']).map(asText);
  const mapping = isRecord(source['attribute_mapping']) ? source['attribute_mapping'] : {};
  return rows.map((row) => {
    const keys = keyColumns.map((column) => row[column]);
    const key: MappedCell = keys.some(isEmpty)
      ? { error: 'The key is empty.' }
      : { value: keys.map(textOf).join(' / ') };
    const cells: Record<string, MappedCell> = {};
    for (const [attribute, type] of Object.entries(types)) {
      const entry = mapping[attribute];
      if (!isRecord(entry)) continue;
      if (typeof entry['expression'] === 'string') {
        cells[attribute] = { error: 'Expression mappings are not read by Scribe yet.' };
        continue;
      }
      try {
        let value: unknown = row[asText(entry['column'])];
        for (const transform of asList(entry['transforms'])) {
          if (isRecord(transform)) value = applyTransform(transform, value, row);
        }
        cells[attribute] = { value: coerce(value, type) };
      } catch (error) {
        cells[attribute] = { error: message(error) };
      }
    }
    return { key, cells };
  });
}
