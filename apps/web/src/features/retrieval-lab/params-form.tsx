import type { Kind, SearchMode } from '@/api/types';
import { retrievalLab as retrievalLabMessages } from '@/messages/retrieval-lab';
import { Input, Select } from '@/ui/field';
import { MODES, needsVector, type LabParams, type ParamsRequest } from './params';

const P = retrievalLabMessages.params;

const NUMBER_FIELDS = [
  'lexicalWeight',
  'semanticWeight',
  'rrfK',
  'trigramThreshold',
  'trigramWeight',
  'typoCandidates',
  'candidates',
  'limit',
] as const satisfies readonly (keyof LabParams & keyof typeof P.fields)[];

/** The parameters of one search; empty fields keep the Domain's configured value. */
export function ParamsForm({
  name,
  params,
  errors,
  kinds,
  hasVector,
  onChange,
}: {
  name: string;
  params: LabParams;
  errors: ParamsRequest['errors'];
  kinds: Kind[];
  hasVector: boolean;
  onChange: (next: LabParams) => void;
}) {
  const set = (patch: Partial<LabParams>) => {
    onChange({ ...params, ...patch });
  };
  const waiting = needsVector(params.mode) && !hasVector;
  return (
    <fieldset className="min-w-0 rounded-md border border-line bg-surface p-4">
      <legend className="px-1 text-sm font-semibold">{P.legend(name)}</legend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Select
          label={P.mode}
          value={params.mode}
          options={MODES.map((mode) => ({
            value: mode,
            label: needsVector(mode) && !hasVector ? P.needsVector(mode) : mode,
          }))}
          onChange={(event) => {
            set({ mode: event.target.value as SearchMode });
          }}
        />
        {NUMBER_FIELDS.map((field) => (
          <Input
            key={field}
            label={P.fields[field].label}
            hint={P.fields[field].hint}
            inputMode="decimal"
            value={params[field]}
            error={errors[field]}
            onChange={(event) => {
              set({ [field]: event.target.value });
            }}
          />
        ))}
      </div>
      {waiting && <p className="mt-2 text-xs text-warning">{P.waiting(params.mode, name)}</p>}
      {kinds.length > 0 && (
        <div role="group" aria-label={P.kinds(name)} className="mt-3 flex flex-wrap gap-3">
          {kinds.map((kind) => (
            <label key={kind.id} className="flex items-center gap-1 text-sm">
              <input
                type="checkbox"
                checked={params.kinds.includes(kind.api_name)}
                onChange={(event) => {
                  set({
                    kinds: event.target.checked
                      ? [...params.kinds, kind.api_name]
                      : params.kinds.filter((k) => k !== kind.api_name),
                  });
                }}
              />
              {kind.display_name}
            </label>
          ))}
          <span className="text-xs text-muted">{P.noKind}</span>
        </div>
      )}
    </fieldset>
  );
}
