import { useState, type FormEvent } from 'react';
import { describeError, ValidationError } from '@/api/errors';
import { useUpdateDomain } from '@/api/queries';
import type { Domain } from '@/api/types';
import { settings as settingsMessages } from '@/messages/settings';
import { Button } from '@/ui/button';
import { Input, Select } from '@/ui/field';
import { Section } from '@/ui/layout';

const T = settingsMessages.extraction;
const MODES = settingsMessages.domain.modes;
const MEMBERS = ['mode', 'auto_accept_threshold', 'min_confidence'] as const;
type Member = (typeof MEMBERS)[number];

/** Members as typed: an empty string leaves the member to the level above. */
type Members = Record<Member, string>;
interface RuleDraft extends Members {
  source_key_prefix: string;
}
interface Draft extends Members {
  rules: RuleDraft[];
}

const EMPTY: Members = { mode: '', auto_accept_threshold: '', min_confidence: '' };
const POINTER = '/body/settings/extraction';

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** A stored member as text; anything but a string shows as JSON (numbers as typed). */
const text = (value: unknown): string =>
  value === undefined || value === null
    ? ''
    : typeof value === 'string'
      ? value
      : JSON.stringify(value);

function membersOf(raw: Record<string, unknown>): Members {
  return {
    mode: text(raw['mode']),
    auto_accept_threshold: text(raw['auto_accept_threshold']),
    min_confidence: text(raw['min_confidence']),
  };
}

/** The form's reading of `settings.extraction`. */
export function draftOf(settings: Record<string, unknown>): Draft {
  const raw = isRecord(settings['extraction']) ? settings['extraction'] : {};
  const rules = Array.isArray(raw['rules']) ? (raw['rules'] as unknown[]) : [];
  return {
    ...membersOf(raw),
    rules: rules.map((rule) => {
      const found = isRecord(rule) ? rule : {};
      return { ...membersOf(found), source_key_prefix: text(found['source_key_prefix']) };
    }),
  };
}

/** Numbers as numbers; anything else is sent as typed for Keeper to judge. */
const valueOf = (member: Member, typed: string): string | number => {
  if (member === 'mode') return typed;
  const number = Number(typed);
  return Number.isNaN(number) ? typed : number;
};

function payloadMembers(members: Members): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const member of MEMBERS) {
    const typed = members[member].trim();
    if (typed !== '') out[member] = valueOf(member, typed);
  }
  return out;
}

/** The `extraction` value to write; `null` when nothing is set, which removes the member. */
export function payloadOf(draft: Draft): Record<string, unknown> | null {
  const out: Record<string, unknown> = payloadMembers(draft);
  if (draft.rules.length > 0) {
    out['rules'] = draft.rules.map((rule) => ({
      source_key_prefix: rule.source_key_prefix,
      ...payloadMembers(rule),
    }));
  }
  return Object.keys(out).length === 0 ? null : out;
}

/** `extraction.rules[0].mode` (an ignored path) as the form's field key `rules/0/mode`. */
export const ignoredKey = (path: string): string =>
  path
    .replace(/^extraction\.?/, '')
    .replace(/\[(\d+)\]/g, '/$1')
    .replace(/\./g, '/');

/** `/body/settings/extraction/rules/0/mode` (a 422 pointer) as the field key `rules/0/mode`. */
export const pointerKey = (pointer: string): string =>
  pointer.startsWith(`${POINTER}/`) ? pointer.slice(POINTER.length + 1) : pointer;

const modeOptions = (inherit: string) => [
  { value: '', label: inherit },
  ...Object.entries(MODES).map(([value, label]) => ({ value, label })),
];

function MemberFields({
  members,
  at,
  inherit,
  errors,
  ignored,
  onChange,
}: {
  members: Members;
  at: string;
  inherit: string;
  errors: Map<string, string>;
  ignored: Set<string>;
  onChange: (member: Member, value: string) => void;
}) {
  const key = (member: Member) => `${at}${member}`;
  const hint = (member: Member, fallback?: string): string | undefined =>
    ignored.has(key(member)) ? T.ignored : fallback;
  const modeHint = hint('mode');
  return (
    <>
      <Select
        className="w-48"
        label={T.mode}
        value={members.mode}
        options={
          members.mode === '' || members.mode in MODES
            ? modeOptions(inherit)
            : [...modeOptions(inherit), { value: members.mode, label: members.mode }]
        }
        error={errors.get(key('mode'))}
        {...(modeHint === undefined ? {} : { hint: modeHint })}
        onChange={(event) => {
          onChange('mode', event.target.value);
        }}
      />
      {(['auto_accept_threshold', 'min_confidence'] as const).map((member) => {
        const fieldHint = hint(
          member,
          member === 'auto_accept_threshold' ? T.thresholdHint : T.minConfidenceHint,
        );
        return (
          <Input
            key={member}
            className="w-56"
            label={member === 'auto_accept_threshold' ? T.threshold : T.minConfidence}
            type="number"
            inputMode="decimal"
            step="0.01"
            min={0}
            max={member === 'auto_accept_threshold' ? 1.01 : 1}
            placeholder={inherit}
            value={members[member]}
            error={errors.get(key(member))}
            {...(fieldHint === undefined ? {} : { hint: fieldHint })}
            onChange={(event) => {
              onChange(member, event.target.value);
            }}
          />
        );
      })}
    </>
  );
}

/**
 * The editable ingestion settings (`settings.extraction`, design 9.2) for a caller who holds
 * `domain.settings`. Keeper validates on save; its findings are shown at the fields they name.
 */
export function ExtractionForm({ domain }: { domain: Domain }) {
  const update = useUpdateDomain(domain.id);
  const [draft, setDraft] = useState<Draft>(() => draftOf(domain.settings));
  const [saved, setSaved] = useState(false);

  const ignored = new Set((domain.ignored_settings ?? []).map(ignoredKey));
  const issues = update.error instanceof ValidationError ? update.error.fields : [];
  const errors = new Map(issues.map((issue) => [pointerKey(issue.pointer), issue.message]));
  const fieldKeys = new Set([
    ...MEMBERS,
    ...draft.rules.flatMap((_, index) =>
      [...MEMBERS, 'source_key_prefix'].map((member) => `rules/${index}/${member}`),
    ),
  ]);
  const elsewhere = issues.filter((issue) => !fieldKeys.has(pointerKey(issue.pointer)));
  const failure =
    update.error !== null && !(update.error instanceof ValidationError)
      ? describeError(update.error)
      : undefined;

  const edit = (next: Draft) => {
    setDraft(next);
    setSaved(false);
  };
  const editRule = (index: number, change: Partial<RuleDraft>) => {
    edit({
      ...draft,
      rules: draft.rules.map((rule, at) => (at === index ? { ...rule, ...change } : rule)),
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSaved(false);
    update.mutate(
      { settings: { extraction: payloadOf(draft) } },
      {
        onSuccess: (updated) => {
          setDraft(draftOf(updated.settings));
          setSaved(true);
        },
      },
    );
  };

  return (
    <Section title={T.title} description={T.description}>
      <form aria-label={T.form} noValidate onSubmit={submit} className="flex flex-col gap-4">
        <div className="flex flex-wrap items-start gap-3">
          <MemberFields
            members={draft}
            at=""
            inherit={T.installationDefault}
            errors={errors}
            ignored={ignored}
            onChange={(member, value) => {
              edit({ ...draft, [member]: value });
            }}
          />
        </div>

        <fieldset className="flex flex-col gap-3">
          <legend className="text-xs font-medium text-muted">{T.rules}</legend>
          <p className="text-xs text-muted">{T.rulesHint}</p>
          {draft.rules.length === 0 && <p className="text-sm text-muted">{T.noRules}</p>}
          {draft.rules.map((rule, index) => (
            <div
              key={index}
              role="group"
              aria-label={T.rule(index)}
              className="flex flex-wrap items-start gap-3 rounded-md border border-line p-3"
            >
              <Input
                className="w-56"
                label={T.prefix}
                value={rule.source_key_prefix}
                error={errors.get(`rules/${index}/source_key_prefix`)}
                onChange={(event) => {
                  editRule(index, { source_key_prefix: event.target.value });
                }}
              />
              <MemberFields
                members={rule}
                at={`rules/${index}/`}
                inherit={T.domainValue}
                errors={errors}
                ignored={ignored}
                onChange={(member, value) => {
                  editRule(index, { [member]: value });
                }}
              />
              <div className="flex flex-col gap-1">
                <span aria-hidden className="invisible text-xs font-medium">
                  &nbsp;
                </span>
                <Button
                  variant="ghost"
                  onClick={() => {
                    edit({ ...draft, rules: draft.rules.filter((_, at) => at !== index) });
                  }}
                >
                  {T.removeRule(index)}
                </Button>
              </div>
            </div>
          ))}
          <div>
            <Button
              size="sm"
              onClick={() => {
                edit({ ...draft, rules: [...draft.rules, { ...EMPTY, source_key_prefix: '' }] });
              }}
            >
              {T.addRule}
            </Button>
          </div>
        </fieldset>

        {(issues.length > 0 || failure !== undefined) && (
          <div role="alert" className="text-sm text-danger">
            {failure ?? T.rejected}
            {elsewhere.length > 0 && (
              <ul className="mt-1 list-disc pl-5">
                {elsewhere.map((issue) => (
                  <li key={`${issue.pointer}:${issue.message}`}>
                    <span className="font-mono text-xs">{pointerKey(issue.pointer)}</span>:{' '}
                    {issue.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {saved && (
          <p role="status" className="text-sm text-success">
            {T.saved}
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" disabled={update.isPending}>
            {update.isPending ? T.saving : T.save}
          </Button>
          <Button
            variant="ghost"
            disabled={update.isPending}
            onClick={() => {
              update.reset();
              edit(draftOf(domain.settings));
            }}
          >
            {T.reset}
          </Button>
        </div>
      </form>
    </Section>
  );
}
