import { useState } from 'react';
import type { KeeperApi } from '@/api/client';
import { useOptionalKeeperApi } from '@/api/provider';
import { useOptionalScope } from '@/app/scope';
import { modelerEditors as modelerEditorsMessages } from '@/messages/modeler-editors';
import type { SchemaDocument } from './document';
import { asText, isRecord, typeLabel } from './form-helpers';
import {
  expressionAttributes,
  flatten,
  mapRows,
  readSample,
  type MappedCell,
  type Sample,
} from './source-preview';
import { ExpressionFeed, type ExpressionEvaluation } from './expression-feed';

const t = modelerEditorsMessages.source.preview;

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

/** Parquet cannot be read in the browser: Keeper reads the file and returns its first rows. */
async function readParquet(
  file: File,
  api: KeeperApi | null,
  domain: string | undefined,
  flattenSeparator: string,
): Promise<Sample> {
  if (api === null || domain === undefined) {
    return { rows: [], error: t.parquetNotRead };
  }
  try {
    const preview = await api.inference.preview(domain, file, { rows: PREVIEW_ROWS });
    return { rows: preview.rows.map((row) => flatten(row, flattenSeparator)) };
  } catch (error) {
    return { rows: [], error: error instanceof Error ? error.message : t.cannotRead };
  }
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
 * The first rows of a sample file as the Source would map them onto the target Kind. CSV, TSV,
 * JSON and JSON Lines files are read in the browser; a Parquet file is sent to Keeper, which
 * returns its first rows and stores nothing. When a mapping uses an expression, the sample rows
 * are sent to Keeper, which evaluates the expressions (transforms and coercion are applied here).
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
  const api = useOptionalKeeperApi();
  const domain = useOptionalScope()?.domain;
  const kind = asText(source['target_kind']);
  const types = mappableTypes(document, kind);
  const columns = Object.keys(types).filter(
    (a) => isRecord(source['attribute_mapping']) && a in source['attribute_mapping'],
  );

  const read = async (file: File) => {
    if (/\.parquet$/i.test(file.name)) {
      const parquet = await readParquet(file, api, domain, asText(source['flatten_separator']));
      setLoaded({ fileName: file.name, sample: parquet });
      return;
    }
    const sample = readSample(file.name, await readFile(file), {
      recordsPath: asText(source['records_path']),
      flattenSeparator: asText(source['flatten_separator']),
      limit: PREVIEW_ROWS,
    });
    setLoaded({ fileName: file.name, sample });
  };
  const [evaluation, setEvaluation] = useState<ExpressionEvaluation>();
  const sampleRows = loaded?.sample.rows ?? [];
  const withExpressions = expressionAttributes(source).length > 0;
  const expressions = withExpressions ? evaluation : undefined;
  // Mapped on every render, so the table follows the edits of the mapping above.
  const rows = loaded === null ? [] : mapRows(sampleRows, source, types, expressions?.results);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted">
        {t.intro(PREVIEW_ROWS, kind === '' ? null : kind)}
        {withExpressions && ` ${t.expressionsNote}`}
      </p>
      <label className="flex flex-col gap-1 text-xs font-medium text-muted">
        {t.file(name)}
        <input
          type="file"
          accept=".csv,.tsv,.json,.jsonl,.ndjson,.parquet"
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
      {withExpressions && sampleRows.length > 0 && (
        <ExpressionFeed
          name={name}
          document={document}
          rows={sampleRows}
          onChange={setEvaluation}
        />
      )}
      {expressions?.error !== undefined && (
        <p role="alert" className="text-sm text-danger">
          {t.expressionsFailed(expressions.error)}
        </p>
      )}
      {loaded !== null && loaded.sample.error === undefined && (
        <div className="overflow-x-auto">
          <table aria-label={t.table(name)} className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th scope="col" className="py-1 pr-4 font-medium">
                  {t.key}
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
          {rows.length === 0 && <p className="text-sm text-muted">{t.noRows(loaded.fileName)}</p>}
        </div>
      )}
    </div>
  );
}
