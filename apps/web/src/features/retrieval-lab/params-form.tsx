import type { Kind, SearchMode } from '@/api/types';
import { Input, Select } from '@/ui/field';
import { MODES, needsVector, type LabParams, type ParamsRequest } from './params';

const NUMBER_FIELDS: { field: keyof LabParams; label: string; hint: string }[] = [
  { field: 'lexicalWeight', label: 'lexical_weight', hint: 'Weight of the word match' },
  { field: 'semanticWeight', label: 'semantic_weight', hint: 'Weight of the vector match' },
  { field: 'rrfK', label: 'rrf_k', hint: 'Rank fusion constant' },
  { field: 'trigramThreshold', label: 'trigram_threshold', hint: 'Similarity for typo matches' },
  { field: 'trigramWeight', label: 'trigram_weight', hint: 'Weight of typo matches' },
  { field: 'typoCandidates', label: 'typo_candidates', hint: 'Typo corrections tried' },
  { field: 'candidates', label: 'candidates', hint: 'Candidates per ranking' },
  { field: 'limit', label: 'limit', hint: 'Results shown' },
];

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
    <fieldset className="rounded-md border border-line bg-surface p-3">
      <legend className="px-1 text-sm font-semibold">Parameters {name}</legend>
      <div className="grid grid-cols-2 gap-3">
        <Select
          label={`Mode ${name}`}
          value={params.mode}
          options={MODES.map((mode) => ({
            value: mode,
            label: needsVector(mode) && !hasVector ? `${mode} (needs a vector)` : mode,
          }))}
          onChange={(event) => {
            set({ mode: event.target.value as SearchMode });
          }}
        />
        {NUMBER_FIELDS.map(({ field, label, hint }) => (
          <Input
            key={field}
            label={`${label} ${name}`}
            hint={hint}
            inputMode="decimal"
            value={params[field] as string}
            error={errors[field]}
            onChange={(event) => {
              set({ [field]: event.target.value });
            }}
          />
        ))}
      </div>
      {waiting && (
        <p className="mt-2 text-xs text-warning">
          {params.mode} needs a query vector. Until there is one, set {name} searches by words only.
        </p>
      )}
      {kinds.length > 0 && (
        <div role="group" aria-label={`Kinds ${name}`} className="mt-3 flex flex-wrap gap-3">
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
          <span className="text-xs text-muted">No Kind ticked: all Kinds.</span>
        </div>
      )}
    </fieldset>
  );
}
