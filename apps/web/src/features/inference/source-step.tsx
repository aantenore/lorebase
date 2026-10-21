import { useState } from 'react';
import type { InferRequest } from '@/api/inference-types';
import { useConfig } from '@/config/context';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Tabs } from '@/ui/tabs';
import { FileDrop } from './file-drop';
import { listOfText } from '@/features/modeler/form-helpers';
import { secretReferenceError } from './postgres';

export interface SourceStepProps {
  pending: boolean;
  error: string | null;
  onRun: (request: InferRequest) => void;
}

function FileSource({ pending, onRun }: Pick<SourceStepProps, 'pending' | 'onRun'>) {
  const { inference } = useConfig();
  const [files, setFiles] = useState<File[]>([]);
  return (
    <div className="flex flex-col gap-4">
      <FileDrop files={files} caps={inference} onChange={setFiles} />
      <div>
        <Button
          variant="primary"
          disabled={files.length === 0 || pending}
          onClick={() => {
            onRun({ files });
          }}
        >
          {pending ? 'Inferring...' : 'Run inference'}
        </Button>
      </div>
    </div>
  );
}

function PostgresSource({ pending, onRun }: Pick<SourceStepProps, 'pending' | 'onRun'>) {
  const { inference } = useConfig();
  const [dsn, setDsn] = useState('');
  const [schema, setSchema] = useState('public');
  const [tables, setTables] = useState('');
  const error = dsn === '' ? undefined : secretReferenceError(dsn);
  const listId = 'postgres-source-suggestions';

  return (
    <form
      className="flex max-w-xl flex-col gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        const names = listOfText(tables);
        onRun({
          postgres: {
            dsn: dsn.trim(),
            ...(schema.trim() === '' ? {} : { schema: schema.trim() }),
            ...(names.length === 0 ? {} : { tables: names }),
          },
        });
      }}
    >
      <Input
        label="Source reference"
        value={dsn}
        list={listId}
        placeholder="secret://env/ERP_DSN"
        hint="A secret reference an administrator allowlisted on the Keeper. Connection strings are never sent."
        error={error}
        onChange={(event) => {
          setDsn(event.target.value);
        }}
      />
      <datalist id={listId}>
        {inference.postgresSources.map((source) => (
          <option key={source} value={source} />
        ))}
      </datalist>
      <Input
        label="Schema"
        value={schema}
        onChange={(event) => {
          setSchema(event.target.value);
        }}
      />
      <Input
        label="Tables"
        value={tables}
        hint="Comma separated. Leave empty for every table of the schema."
        onChange={(event) => {
          setTables(event.target.value);
        }}
      />
      <div>
        <Button
          type="submit"
          variant="primary"
          disabled={dsn.trim() === '' || error !== undefined || pending}
        >
          {pending ? 'Inferring...' : 'Run inference'}
        </Button>
      </div>
    </form>
  );
}

/** First step: where the data is (data files or a Postgres source) and the run button. */
export function SourceStep({ pending, error, onRun }: SourceStepProps) {
  const [source, setSource] = useState('csv');
  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-3xl text-sm text-muted">
        Point Lorebase at the tables you already have and review the schema it proposes: Kinds,
        typed Attributes, keys and Relations, each with the evidence behind it. The inference reads
        the data with rules only and commits nothing.
      </p>
      <Tabs
        label="Data source"
        value={source}
        onChange={setSource}
        tabs={[
          {
            id: 'csv',
            label: 'Files',
            content: <FileSource pending={pending} onRun={onRun} />,
          },
          {
            id: 'postgres',
            label: 'Postgres source',
            content: <PostgresSource pending={pending} onRun={onRun} />,
          },
        ]}
      />
      {error !== null && (
        <p role="alert" className="text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
