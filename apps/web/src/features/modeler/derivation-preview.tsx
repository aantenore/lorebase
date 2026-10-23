import { useMutation } from '@tanstack/react-query';
import { ValidationError } from '@/api/errors';
import type { DerivationPreview as Preview } from '@/api/preview-types';
import type { KeeperApi } from '@/api/client';
import { useOptionalKeeperApi } from '@/api/provider';
import { useRecords } from '@/api/queries';
import { useScope } from '@/app/scope';
import { ValueView } from '@/features/explorer/value-view';
import { modelerEditors as modelerEditorsMessages } from '@/messages/modeler-editors';
import { Button } from '@/ui/button';
import { Badge } from '@/ui/layout';
import type { SchemaDocument } from './document';

const t = modelerEditorsMessages.derivation.preview;

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
      <table aria-label={t.samples(kind, attribute)} className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-muted">
            <th scope="col" className="py-1 pr-4 font-medium">
              {t.record}
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
                        <Badge>{t.masked}</Badge>
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
        {t.reading}
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
      <p className="text-xs text-muted">{t.saved(attribute, SAMPLE_RECORDS)}</p>
      {noValues && <p className="text-xs text-muted">{t.noValues(attribute)}</p>}
      {rows.length === 0 ? (
        <p className="text-sm text-muted">{t.noRecords(kind)}</p>
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
  return code === null ? t.cannotCompute : t.cannotComputeCode(code);
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
    return <p className="text-xs text-muted">{t.notServed}</p>;
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
      <p className="text-xs text-muted">{t.edited(SAMPLE_RECORDS)}</p>
      <div>
        <Button
          disabled={run.isPending}
          onClick={() => {
            run.mutate();
          }}
        >
          {t.run}
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
            {t.noRecords(kind)}
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
