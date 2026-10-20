/**
 * Type-aware checks on Attribute values, shared by the forms that edit values and by the
 * mock adapter that re-validates them: validation against a type and its constraints
 * (design 5.4) and the cast rules of type changes.
 */
import type { AttributeConstraints, AttributeType, JsonValue, ScalarType } from './types';

export const SCALAR_TYPES: readonly ScalarType[] = [
  'string',
  'text',
  'integer',
  'decimal',
  'float',
  'boolean',
  'date',
  'timestamp',
  'duration',
  'enum',
  'json',
  'geo_point',
  'uri',
];

export interface ParsedType {
  base: ScalarType | 'ref';
  array: boolean;
  /** Target Kind api name of a `ref(Kind)` type. */
  ref?: string;
  precision?: number;
  scale?: number;
}

/** Parse the spelling of a type (`string`, `array<enum>`, `decimal(12,2)`, `ref(Supplier)`). */
export function parseAttributeType(type: string): ParsedType | null {
  const array = /^array<(.+)>$/.exec(type);
  if (array?.[1] !== undefined) {
    const inner = parseAttributeType(array[1]);
    return inner === null || inner.array ? null : { ...inner, array: true };
  }
  const decimal = /^decimal\((\d+),(\d+)\)$/.exec(type);
  if (decimal?.[1] !== undefined && decimal[2] !== undefined) {
    return {
      base: 'decimal',
      array: false,
      precision: Number(decimal[1]),
      scale: Number(decimal[2]),
    };
  }
  const ref = /^ref\(([A-Za-z][A-Za-z0-9]*)\)$/.exec(type);
  if (ref?.[1] !== undefined) return { base: 'ref', array: false, ref: ref[1] };
  const scalar = SCALAR_TYPES.find((candidate) => candidate === type);
  return scalar === undefined ? null : { base: scalar, array: false };
}

export interface ValueSpec {
  type: AttributeType;
  required?: boolean;
  constraints?: AttributeConstraints;
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DURATION = /^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$/;

function validTimestamp(text: string): boolean {
  return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text) && !Number.isNaN(Date.parse(text));
}

function validDate(text: string): boolean {
  if (!DATE.test(text)) return false;
  const parsed = new Date(`${text}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === text;
}

function inRange(value: number, constraints: AttributeConstraints): string | null {
  if (constraints.min !== undefined && value < constraints.min) {
    return `Must be at least ${constraints.min}.`;
  }
  if (constraints.max !== undefined && value > constraints.max) {
    return `Must be at most ${constraints.max}.`;
  }
  return null;
}

function checkText(value: string, constraints: AttributeConstraints): string | null {
  if (constraints.min_length !== undefined && value.length < constraints.min_length) {
    return `Must have at least ${constraints.min_length} characters.`;
  }
  if (constraints.max_length !== undefined && value.length > constraints.max_length) {
    return `Must have at most ${constraints.max_length} characters.`;
  }
  if (constraints.pattern !== undefined) {
    try {
      if (!new RegExp(constraints.pattern).test(value)) {
        return `Must match ${constraints.pattern}.`;
      }
    } catch {
      return null;
    }
  }
  return null;
}

function checkScalar(
  base: ParsedType['base'],
  value: JsonValue,
  constraints: AttributeConstraints,
): string | null {
  switch (base) {
    case 'string':
    case 'text':
      return typeof value === 'string' ? checkText(value, constraints) : 'Must be text.';
    case 'uri':
      if (typeof value !== 'string') return 'Must be text.';
      return URL.canParse(value) ? checkText(value, constraints) : 'Must be a valid URI.';
    case 'integer':
      return typeof value === 'number' && Number.isInteger(value)
        ? inRange(value, constraints)
        : 'Must be a whole number.';
    case 'decimal':
    case 'float':
      return typeof value === 'number' && Number.isFinite(value)
        ? inRange(value, constraints)
        : 'Must be a number.';
    case 'boolean':
      return typeof value === 'boolean' ? null : 'Must be true or false.';
    case 'date':
      return typeof value === 'string' && validDate(value) ? null : 'Must be a date (YYYY-MM-DD).';
    case 'timestamp':
      return typeof value === 'string' && validTimestamp(value)
        ? null
        : 'Must be an ISO 8601 timestamp.';
    case 'duration':
      return typeof value === 'string' && DURATION.test(value)
        ? null
        : 'Must be an ISO 8601 duration.';
    case 'enum':
      if (typeof value !== 'string') return 'Must be one of the listed values.';
      return constraints.enum_values === undefined || constraints.enum_values.includes(value)
        ? null
        : `Must be one of: ${constraints.enum_values.join(', ')}.`;
    case 'geo_point': {
      const point = Array.isArray(value) ? value : [];
      const [lat, lon] = point;
      return point.length === 2 &&
        typeof lat === 'number' &&
        typeof lon === 'number' &&
        Math.abs(lat) <= 90 &&
        Math.abs(lon) <= 180
        ? null
        : 'Must be [latitude, longitude].';
    }
    case 'ref':
      return typeof value === 'string' && value !== '' ? null : 'Must be a Record key.';
    case 'json':
      return null;
  }
}

/** The reason `value` does not fit `spec`, or `null` when it does. `null` means absent. */
export function validateValue(spec: ValueSpec, value: JsonValue | undefined): string | null {
  if (value === null || value === undefined) {
    return spec.required === true ? 'A value is required.' : null;
  }
  const parsed = parseAttributeType(spec.type);
  if (parsed === null) return `Unknown type ${spec.type}.`;
  const constraints = spec.constraints ?? {};
  if (!parsed.array) return checkScalar(parsed.base, value, constraints);
  if (!Array.isArray(value)) return 'Must be a list.';
  for (const item of value) {
    const problem = checkScalar(parsed.base, item, constraints);
    if (problem !== null) return problem;
  }
  return null;
}

export type CastRule = 'safe' | 'checked' | 'forbidden';

const TEXTUAL: readonly ParsedType['base'][] = ['string', 'text', 'uri', 'ref', 'enum'];
const NUMERIC: readonly ParsedType['base'][] = ['integer', 'decimal', 'float'];

function baseRule(from: ParsedType['base'], to: ParsedType['base']): CastRule {
  if (from === to) return 'safe';
  if (to === 'json') return 'safe';
  if (to === 'string' || to === 'text') return from === 'json' ? 'checked' : 'safe';
  if (from === 'json') return 'forbidden';
  if (TEXTUAL.includes(from)) return to === 'geo_point' ? 'forbidden' : 'checked';
  if (from === 'integer' && (to === 'decimal' || to === 'float')) return 'safe';
  if (NUMERIC.includes(from) && NUMERIC.includes(to)) return 'checked';
  if (from === 'date' && to === 'timestamp') return 'safe';
  if (from === 'timestamp' && to === 'date') return 'checked';
  return 'forbidden';
}

/** Cast rule of a type change (design 5.4): `safe`, `checked` per value, or `forbidden`. */
export function castRule(from: AttributeType, to: AttributeType): CastRule {
  const source = parseAttributeType(from);
  const target = parseAttributeType(to);
  if (source === null || target === null) return 'forbidden';
  if (source.array !== target.array) return 'forbidden';
  return baseRule(source.base, target.base);
}

type CastResult = { ok: true; value: JsonValue } | { ok: false };

const FAIL: CastResult = { ok: false };

function castScalar(
  value: JsonValue,
  to: ParsedType['base'],
  constraints: AttributeConstraints,
): CastResult {
  const done = (cast: JsonValue): CastResult => ({ ok: true, value: cast });
  switch (to) {
    case 'string':
    case 'text':
      return done(typeof value === 'string' ? value : JSON.stringify(value));
    case 'json':
      return done(value);
    case 'integer': {
      const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
      return typeof number === 'number' && Number.isInteger(number) ? done(number) : FAIL;
    }
    case 'decimal':
    case 'float': {
      const number = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
      return typeof number === 'number' && Number.isFinite(number) ? done(number) : FAIL;
    }
    case 'boolean':
      if (value === 'true') return done(true);
      return value === 'false' ? done(false) : FAIL;
    case 'date': {
      const day = typeof value === 'string' ? value.slice(0, 10) : '';
      return validDate(day) ? done(day) : FAIL;
    }
    case 'timestamp':
      if (typeof value !== 'string') return FAIL;
      if (validDate(value)) return done(`${value}T00:00:00Z`);
      return validTimestamp(value) ? done(value) : FAIL;
    case 'enum':
      return typeof value === 'string' && checkScalar('enum', value, constraints) === null
        ? done(value)
        : FAIL;
    case 'uri':
      return typeof value === 'string' && URL.canParse(value) ? done(value) : FAIL;
    case 'duration':
      return typeof value === 'string' && DURATION.test(value) ? done(value) : FAIL;
    case 'ref':
      return typeof value === 'string' && value !== '' ? done(value) : FAIL;
    case 'geo_point':
      return FAIL;
  }
}

/** Cast one stored value to a new type; `ok: false` when the value does not survive. */
export function castValue(
  value: JsonValue,
  from: AttributeType,
  to: AttributeType,
  toConstraints: AttributeConstraints = {},
): CastResult {
  if (castRule(from, to) === 'forbidden') return FAIL;
  const target = parseAttributeType(to);
  if (target === null) return FAIL;
  if (!target.array) return castScalar(value, target.base, toConstraints);
  if (!Array.isArray(value)) return FAIL;
  const items: JsonValue[] = [];
  for (const item of value) {
    const cast = castScalar(item, target.base, toConstraints);
    if (!cast.ok) return FAIL;
    items.push(cast.value);
  }
  return { ok: true, value: items };
}
