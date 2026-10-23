import { useState } from 'react';
import type { InferRequest } from '@/api/inference-types';
import { useConfig } from '@/config/context';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { LearnMore } from '@/ui/learn-more';
import { Tabs } from '@/ui/tabs';
import { FileDrop } from './file-drop';
import { listOfText } from '@/features/modeler/form-helpers';
import { secretReferenceError } from './postgres';
import { inference as inferenceMessages } from '@/messages/inference';

export interface SourceStepProps {
  pending: boolean;
  error: string | null;
  onRun: (request: InferRequest) => void;
}

function FileSource({ pending, onRun }: Pick<SourceStepProps, 'pending' | 'onRun'>) {
  const { inference } = useConfig();
  const [files, setFiles] = useState<File[]>([]);
  const [recordsPath, setRecordsPath] = useState('');
  return (
    <div className="flex flex-col gap-4">
      <FileDrop files={files} caps={inference} onChange={setFiles} />
      <Input
        label={inferenceMessages.source.recordsPath.label}
        value={recordsPath}
        placeholder={inferenceMessages.source.recordsPath.placeholder}
        hint={inferenceMessages.source.recordsPath.hint}
        onChange={(event) => {
          setRecordsPath(event.target.value);
        }}
      />
      <div>
        <Button
          variant="primary"
          disabled={files.length === 0 || pending}
          onClick={() => {
            onRun(
              recordsPath.trim() === '' ? { files } : { files, recordsPath: recordsPath.trim() },
            );
          }}
        >
          {pending ? inferenceMessages.source.running : inferenceMessages.source.run}
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
        label={inferenceMessages.source.reference.label}
        value={dsn}
        list={listId}
        placeholder={inferenceMessages.source.reference.placeholder}
        hint={inferenceMessages.source.reference.hint}
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
        label={inferenceMessages.source.schema}
        value={schema}
        onChange={(event) => {
          setSchema(event.target.value);
        }}
      />
      <Input
        label={inferenceMessages.source.tables.label}
        value={tables}
        hint={inferenceMessages.source.tables.hint}
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
          {pending ? inferenceMessages.source.running : inferenceMessages.source.run}
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
        {inferenceMessages.source.intro} <LearnMore path={inferenceMessages.docs} />
      </p>
      <Tabs
        label={inferenceMessages.source.tabsLabel}
        value={source}
        onChange={setSource}
        tabs={[
          {
            id: 'csv',
            label: inferenceMessages.source.filesTab,
            content: <FileSource pending={pending} onRun={onRun} />,
          },
          {
            id: 'postgres',
            label: inferenceMessages.source.postgresTab,
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
