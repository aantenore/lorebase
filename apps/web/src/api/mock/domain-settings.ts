import { apiErrorFromProblem } from '../errors';

/**
 * The mock's reading of a Domain's `extraction` setting (design 9.2), with the installation
 * defaults Keeper ships: `doubts_only`, threshold 0.9, `min_confidence` 0.3, and only `mode`
 * may be loosened. Enough to exercise the Settings form; Keeper is the reference.
 */
const MODES = ['direct', 'doubts_only', 'review_all'] as const;
const MEMBERS = ['mode', 'auto_accept_threshold', 'min_confidence'] as const;
const BASE = { mode: 'doubts_only', auto_accept_threshold: 0.9, min_confidence: 0.3 } as const;
const MAY_LOOSEN = new Set<string>(['mode']);
const MAX_THRESHOLD = 1.01;

type Member = (typeof MEMBERS)[number];
type Issue = { pointer: string; message: string; code: string };

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

function valid(member: Member, value: unknown): boolean {
  if (member === 'mode') return MODES.includes(value as (typeof MODES)[number]);
  if (typeof value !== 'number') return false;
  return value >= 0 && value <= (member === 'auto_accept_threshold' ? MAX_THRESHOLD : 1);
}

function looser(member: Member, value: unknown): boolean {
  if (member === 'mode')
    return MODES.indexOf(value as (typeof MODES)[number]) < MODES.indexOf(BASE.mode);
  if (member === 'auto_accept_threshold') return Number(value) < BASE.auto_accept_threshold;
  return Number(value) > BASE.min_confidence;
}

function membersProblems(raw: Record<string, unknown>, at: string, keys: Set<string>): Issue[] {
  const found: Issue[] = [];
  for (const [key, value] of Object.entries(raw)) {
    if (!keys.has(key)) {
      found.push({ pointer: `${at}/${key}`, message: 'unknown member', code: 'invalid' });
    } else if ((MEMBERS as readonly string[]).includes(key) && !valid(key as Member, value)) {
      const message =
        key === 'mode' ? `must be one of ${MODES.join(', ')}` : 'must be a number from 0 to 1';
      found.push({ pointer: `${at}/${key}`, message, code: 'invalid' });
    }
  }
  return found;
}

/** Problems of a written `extraction` value, at `/body/settings/extraction/...`. */
export function extractionProblems(raw: unknown): Issue[] {
  const at = '/body/settings/extraction';
  if (raw === null) return [];
  if (!isRecord(raw)) return [{ pointer: at, message: 'must be an object', code: 'invalid' }];
  const found = membersProblems(raw, at, new Set([...MEMBERS, 'rules']));
  const rules = raw['rules'];
  if (rules === undefined) return found;
  if (!Array.isArray(rules)) {
    return [...found, { pointer: `${at}/rules`, message: 'must be a list', code: 'invalid' }];
  }
  const seen = new Set<string>();
  rules.forEach((rule: unknown, index) => {
    const ruleAt = `${at}/rules/${index}`;
    if (!isRecord(rule)) {
      found.push({ pointer: ruleAt, message: 'must be an object', code: 'invalid' });
      return;
    }
    const prefix = rule['source_key_prefix'];
    if (typeof prefix !== 'string' || prefix === '') {
      found.push({
        pointer: `${ruleAt}/source_key_prefix`,
        message: 'must be a non-empty string',
        code: 'invalid',
      });
    } else if (seen.has(prefix)) {
      found.push({
        pointer: `${ruleAt}/source_key_prefix`,
        message: 'is given by another rule',
        code: 'invalid',
      });
    } else {
      seen.add(prefix);
    }
    found.push(...membersProblems(rule, ruleAt, new Set([...MEMBERS, 'source_key_prefix'])));
  });
  return found;
}

export function rejectedSettings(issues: Issue[]) {
  return apiErrorFromProblem({
    type: 'urn:lorebase:problem:validation-failed',
    title: 'Request validation failed',
    status: 422,
    code: 'validation-failed',
    detail: 'One or more request members are invalid.',
    errors: issues,
  });
}

function ignoredMembers(raw: Record<string, unknown>, path: string): string[] {
  return MEMBERS.filter(
    (member) =>
      member in raw &&
      (!valid(member, raw[member]) || (looser(member, raw[member]) && !MAY_LOOSEN.has(member))),
  ).map((member) => `${path}.${member}`);
}

/** Paths of the values of the `extraction` setting that do not apply, as Keeper reports them. */
export function ignoredSettings(settings: Record<string, unknown>): string[] {
  const raw = settings['extraction'];
  if (raw === undefined || raw === null) return [];
  if (!isRecord(raw)) return ['extraction'];
  const ignored = ignoredMembers(raw, 'extraction');
  const rules = raw['rules'];
  (Array.isArray(rules) ? rules : []).forEach((rule: unknown, index) => {
    if (isRecord(rule)) ignored.push(...ignoredMembers(rule, `extraction.rules[${index}]`));
  });
  return ignored;
}
