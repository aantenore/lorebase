import { useId, useRef, useState, type DragEvent } from 'react';
import { Button } from '@/ui/button';
import { cn } from '@/ui/cn';
import { addFiles, formatBytes, type Rejected, type UploadCaps } from './files';

export interface FileDropProps {
  files: File[];
  caps: UploadCaps;
  onChange: (files: File[]) => void;
}

/** Drop zone and file picker for the CSV files, with the caps stated and enforced. */
export function FileDrop({ files, caps, onChange }: FileDropProps) {
  const input = useRef<HTMLInputElement>(null);
  const hintId = useId();
  const [over, setOver] = useState(false);
  const [rejected, setRejected] = useState<Rejected[]>([]);

  const take = (incoming: File[]) => {
    const result = addFiles(files, incoming, caps);
    setRejected(result.rejected);
    onChange(result.files);
  };

  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setOver(false);
    take(Array.from(event.dataTransfer.files));
  };

  const total = files.reduce((sum, file) => sum + file.size, 0);

  return (
    <div className="flex flex-col gap-3">
      <div
        role="group"
        aria-label="Drop zone for CSV files"
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => {
          setOver(false);
        }}
        onDrop={onDrop}
        className={cn(
          'flex flex-col items-center gap-2 rounded-md border-2 border-dashed p-6 text-center',
          over ? 'border-accent bg-surface-2' : 'border-line bg-surface',
        )}
      >
        <p className="text-sm">Drop CSV or TSV files here, or</p>
        <Button
          onClick={() => {
            input.current?.click();
          }}
        >
          Choose files
        </Button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-label="Upload CSV or TSV files"
          aria-describedby={hintId}
          accept={caps.fileSuffixes.join(',')}
          onChange={(event) => {
            take(Array.from(event.target.files ?? []));
            event.target.value = '';
          }}
        />
        <p id={hintId} className="text-xs text-muted">
          The file name is the table name. Up to {caps.maxFiles} files,{' '}
          {formatBytes(caps.maxFileBytes)} each and {formatBytes(caps.maxTotalBytes)} together (
          {caps.fileSuffixes.join(', ')}).
        </p>
      </div>

      {rejected.length > 0 && (
        <ul role="alert" aria-label="Rejected files" className="text-sm text-danger">
          {rejected.map((item) => (
            <li key={item.name}>
              {item.name}: {item.reason}
            </li>
          ))}
        </ul>
      )}

      {files.length > 0 && (
        <>
          <ul aria-label="Selected files" className="flex flex-col gap-1 text-sm">
            {files.map((file) => (
              <li key={file.name} className="flex items-center gap-3">
                <span className="font-medium">{file.name}</span>
                <span className="text-muted">{formatBytes(file.size)}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove ${file.name}`}
                  onClick={() => {
                    onChange(files.filter((f) => f !== file));
                  }}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted">
            {files.length} {files.length === 1 ? 'file' : 'files'}, {formatBytes(total)}
          </p>
        </>
      )}
    </div>
  );
}
