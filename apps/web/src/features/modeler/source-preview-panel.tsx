import { useState } from 'react';
import type { SchemaDocument } from './document';
import { asText, isRecord, typeLabel } from './form-helpers';
import { mapRows, readSample, type MappedCell, type Sample } from './source-preview';

/** Rows shown, and the part of the file read to find them. */
const PREVIEW_ROWS = 5;
const READ_BYTES = 1_000_000;

type Doc = Record<string, unknown>;

interface Loaded {
  fileName: string;
  sample: Sample;
}

/** Types of the Attributes of the target Kind that a Source can map: not the derived ones. */
function mappableTypes(document: SchemaDocument, kind: string): Record<string, string> {
  const attributes = document.kinds?.[kind]?.attributes ?? {};
  return Object.fromEntries(
    Object.entries(attributes)
      .filter(([, attribute]) => attribute.derivation === undefined)
      .map(([name, attribute]) => [name, typeLabel(attribute.type)]),
  );
}

async function readFile(file: File): Promise<string> {
  const part = file.slice(0, READ_BYTES);
  const text = await part.text();
  // A file cut short ends in the middle of a line: keep the whole lines of line formats.
  const lineFormat = /\.(csv|tsv|jsonl|ndjson)$/i.test(file.name);
  return file.size > READ_BYTES && lineFormat ? text.slice(0, text.lastIndexOf('\n')) : text;
}

function Cell({ cell }: { cell: MappedCell }) {
  if (cell.error !== undefined) return <span className="text-danger">{cell.error}</span>;
  if (cell.value === null || cell.value === undefined) return <span className="text-muted">-</span>;
  return <>{cell.value}</>;
}

/**
 * The first rows of a sample file as the Source would map them onto the target Kind. The file is
 * read in the browser only; nothing is sent to Keeper.
 */
export function SourcePreview({
  name,
  source,
  document,
}: {
  name: string;
  source: Doc;
  document: SchemaDocument;
}) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const kind = asText(source['target_kind']);
  const types = mappableTypes(document, kind);
  const columns = Object.keys(types).filter(
    (a) => isRecord(source['attribute_mapping']) && a in source['attribute_mapping'],
  );

  const read = async (file: File) => {
    const sample = readSample(file.name, await readFile(file), {
      recordsPath: asText(source['records_path']),
      flattenSeparator: asText(source['flatten_separator']),
      limit: PREVIEW_ROWS,
    });
    setLoaded({ fileName: file.name, sample });
  };
  // Mapped on every render, so the table follows the edits of the mapping above.
  const rows = loaded === null ? [] : mapRows(loaded.sample.rows, source, types);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        Pick a sample of the data (CSV, TSV, JSON or JSON Lines) to see its first {PREVIEW_ROWS}{' '}
        rows mapped onto {kind === '' ? 'the target Kind' : kind} by the mapping above, as edited.
        The file is read in this browser and is not uploaded.
      </p>
      <label className="flex flex-col gap-1 text-xs font-medium text-muted">
        Sample file for {name}
        <input
          type="file"
          accept=".csv,.tsv,.json,.jsonl,.ndjson"
          className="text-sm text-ink"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file !== undefined) void read(file);
          }}
        />
      </label>
      {loaded?.sample.error !== undefined && (
        <p role="alert" className="text-sm text-danger">
          {loaded.sample.error}
        </p>
      )}
      {loaded !== null && loaded.sample.error === undefined && (
        <div className="overflow-x-auto">
          <table aria-label={`Sample rows of ${name}`} className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th scope="col" className="py-1 pr-4 font-medium">
                  Key
                </th>
                {columns.map((attribute) => (
                  <th key={attribute} scope="col" className="py-1 pr-4 font-medium">
                    {attribute}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={index} className="border-t border-line align-top">
                  <th scope="row" className="py-1 pr-4 text-left font-normal">
                    <Cell cell={row.key} />
                  </th>
                  {columns.map((attribute) => (
                    <td key={attribute} className="py-1 pr-4">
                      <Cell cell={row.cells[attribute] ?? {}} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && (
            <p className="text-sm text-muted">{loaded.fileName} has no rows.</p>
          )}
        </div>
      )}
    </div>
  );
}
