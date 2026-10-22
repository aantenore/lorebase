import { useRecords } from '@/api/queries';
import { useScope } from '@/app/scope';
import { ValueView } from '@/features/explorer/value-view';

/** Records shown by the preview of a Derivation. */
const SAMPLE_RECORDS = 5;

/**
 * The saved Derivation at work: the computed Attribute on the first few Records of the target
 * Kind, as Keeper reads them on the current Branch (masking and permissions are Keeper's).
 * It shows the Derivation as saved, so an edit shows here once the change set is saved.
 */
export function DerivationPreview({ kind, attribute }: { kind: string; attribute: string }) {
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
                    <ValueView value={valueOf(row)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
