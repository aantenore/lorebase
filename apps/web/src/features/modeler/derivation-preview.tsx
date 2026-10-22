import { useMutation } from '@tanstack/react-query';
import { ValidationError } from '@/api/errors';
import type { DerivationPreview as Preview } from '@/api/preview-types';
import type { KeeperApi } from '@/api/client';
import { useOptionalKeeperApi } from '@/api/provider';
import { useRecords } from '@/api/queries';
import { useScope } from '@/app/scope';
import { ValueView } from '@/features/explorer/value-view';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import type { SchemaDocument } from './document';

/** Records shown by the preview of a Derivation. */
const SAMPLE_RECORDS = 5;

interface Props {
  /** Api name of the Derivation. */
  name: string;
  kind: string;
  attribute: string;
  /** The schema document as edited. */
  document: SchemaDocument;
  /** Whether the Derivation is saved on the Branch exactly as shown. */
  saved: boolean;
}

function SampleTable({
  kind,
  attribute,
  rows,
}: {
  kind: string;
  attribute: string;
  rows: { key: string; title: string; value: unknown; masked?: boolean; error?: string }[];
}) {
  return (
    <div className="overflow-x-auto">
      <table aria-label={`Sample values of ${kind}.${attribute}`} className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className="py-1 pr-4 font-medium">
              Record
            </th>
            <th scope="col" className="py-1 font-medium">
              {attribute}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-line">
              <th scope="row" className="py-1 pr-4 text-left font-normal">
                {row.title === '' ? row.key : row.title}
              </th>
              <td className="py-1">
                {row.error !== undefined ? (
                  <span className="text-danger">{row.error}</span>
                ) : (
                  <>
                    <ValueView value={row.value} />
                    {row.masked === true && (
                      <>
                        {' '}
                        <Badge>masked</Badge>
                      </>
                    )}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A Derivation saved on the Branch at work: the values Keeper holds for it, read as Records. */
function SavedPreview({ kind, attribute }: { kind: string; attribute: string }) {
  const scope = useScope();
  const records = useRecords(scope, kind, { limit: SAMPLE_RECORDS });
  if (records.isPending) {
    return (
      <p role="status" className="text-sm text-muted">
        Reading sample Records...
      </p>
    );
  }
  if (records.isError) {
    return (
      <p role="alert" className="text-sm text-danger">
        {records.error.message}
      </p>
    );
  }
  const rows = records.data.rows.slice(0, SAMPLE_RECORDS);
  const valueOf = (row: (typeof rows)[number]) =>
    row.derived[attribute] ?? row.attributes[attribute];
  const noValues = rows.length > 0 && rows.every((row) => valueOf(row) == null);
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted">
        {attribute} of the first {SAMPLE_RECORDS} {kind} Records on {scope.branch}, computed by the
        saved Derivation.
      </p>
      {noValues && (
        <p className="text-xs text-muted">
          Keeper returned no value of {attribute} for these Records: none is computed yet, or this
          Keeper does not serve derived values.
        </p>
      )}
      {rows.length === 0 ? (
        <p className="text-sm text-muted">No {kind} Records on this Branch.</p>
      ) : (
        <SampleTable
          kind={kind}
          attribute={attribute}
          rows={rows.map((row) => ({ key: row.key, title: row.title, value: valueOf(row) }))}
        />
      )}
    </div>
  );
}

function cannotCompute(code: string | null): string {
  return code === null ? 'Cannot be computed.' : `Cannot be computed (${code}).`;
}

function rowsOf(preview: Preview) {
  return preview.items.map((item) => ({
    key: item.key,
    title: item.title ?? '',
    value: item.value,
    masked: item.masked,
    ...(item.error === null ? {} : { error: item.error.message ?? cannotCompute(item.error.code) }),
  }));
}

/**
 * A Derivation as edited and not saved yet: Keeper checks the edited document like a schema
 * change and evaluates the Derivation on the first Records of the Kind on the Branch, with its
 * own permissions and masking. Nothing is written. Remounted on every edit, so a result never
 * outlives the text it was computed from.
 */
function EditedPreview(props: Omit<Props, 'saved'>) {
  const api = useOptionalKeeperApi();
  if (api === null) {
    return (
      <p className="text-xs text-muted">
        The edited Derivation is evaluated by Keeper, which this view is not using.
      </p>
    );
  }
  return <KeeperPreview api={api} {...props} />;
}

function KeeperPreview({
  api,
  name,
  attribute,
  document,
  kind,
}: Omit<Props, 'saved'> & { api: KeeperApi }) {
  const scope = useScope();
  const run = useMutation({
    mutationFn: () =>
      api.derivations.preview(scope, { document, derivation: name, limit: SAMPLE_RECORDS }),
  });
  const issues = run.error instanceof ValidationError ? run.error.issues.slice(0, 5) : [];
  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        Evaluates {name} as edited, without saving it, on the first {SAMPLE_RECORDS} {kind} Records
        of {scope.branch}. Keeper checks the edit first, as it checks a change set.
      </p>
      <div>
        <Button
          disabled={run.isPending}
          onClick={() => {
            run.mutate();
          }}
        >
          Preview the edited Derivation
        </Button>
      </div>
      {run.isError && (
        <div role="alert" className="flex flex-col gap-1 text-sm text-danger">
          <p>{run.error.message}</p>
          {issues.length > 0 && (
            <ul className="list-disc pl-5">
              {issues.map((issue, index) => (
                <li key={index}>
                  {issue.path === undefined ? '' : `${issue.path.join('/')}: `}
                  {issue.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {run.data !== undefined &&
        (run.data.items.length === 0 ? (
          <p role="status" className="text-sm text-muted">
            No {kind} Records on this Branch.
          </p>
        ) : (
          <SampleTable kind={kind} attribute={attribute} rows={rowsOf(run.data)} />
        ))}
    </div>
  );
}

/**
 * The computed Attribute on the first few Records of the target Kind. A Derivation saved as
 * shown is read as Records (Keeper's masking and permissions apply); an edited one is
 * evaluated by Keeper without being saved.
 */
export function DerivationPreview({ name, kind, attribute, document, saved }: Props) {
  if (saved) return <SavedPreview kind={kind} attribute={attribute} />;
  return (
    <EditedPreview
      key={JSON.stringify(document)}
      name={name}
      kind={kind}
      attribute={attribute}
      document={document}
    />
  );
}
