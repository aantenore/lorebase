import { useId, useRef, useState, type DragEvent } from 'react';
import { Button } from '@/ui/button';
import { cn } from '@/ui/cn';
import { addFiles, formatBytes, type Rejected, type UploadCaps } from './files';
import { inference as inferenceMessages } from '@/messages/inference';

/** What the drop zone says; the defaults are those of the data file upload of "Start from data". */
export interface FileDropWording {
  /** Accessible name of the drop zone. */
  zoneLabel: string;
  /** Invitation above the button. */
  prompt: string;
  /** Accessible name of the hidden file input. */
  inputLabel: string;
  /** Sentence before the caps. */
  note: string;
}

const DATA_FILE_WORDING: FileDropWording = {
  zoneLabel: inferenceMessages.drop.zoneLabel,
  prompt: inferenceMessages.drop.prompt,
  inputLabel: inferenceMessages.drop.inputLabel,
  note: inferenceMessages.drop.note,
};

export interface FileDropProps {
  files: File[];
  caps: UploadCaps;
  onChange: (files: File[]) => void;
  wording?: FileDropWording;
}

/** Drop zone and file picker, with the caps stated and enforced. */
export function FileDrop({ files, caps, onChange, wording = DATA_FILE_WORDING }: FileDropProps) {
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
        aria-label={wording.zoneLabel}
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
        <p className="text-sm">{wording.prompt}</p>
        <Button
          onClick={() => {
            input.current?.click();
          }}
        >
          {inferenceMessages.drop.choose}
        </Button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-label={wording.inputLabel}
          aria-describedby={hintId}
          accept={caps.fileSuffixes.join(',')}
          onChange={(event) => {
            take(Array.from(event.target.files ?? []));
            event.target.value = '';
          }}
        />
        <p id={hintId} className="text-xs text-muted">
          {wording.note}{' '}
          {inferenceMessages.drop.caps(
            caps.maxFiles,
            formatBytes(caps.maxFileBytes),
            Number.isFinite(caps.maxTotalBytes) ? formatBytes(caps.maxTotalBytes) : null,
            caps.fileSuffixes.join(', '),
          )}
        </p>
      </div>

      {rejected.length > 0 && (
        <ul
          role="alert"
          aria-label={inferenceMessages.drop.rejectedLabel}
          className="text-sm text-danger"
        >
          {rejected.map((item) => (
            <li key={item.name}>
              {item.name}: {item.reason}
            </li>
          ))}
        </ul>
      )}

      {files.length > 0 && (
        <>
          <ul
            aria-label={inferenceMessages.drop.selectedLabel}
            className="flex flex-col gap-1 text-sm"
          >
            {files.map((file) => (
              <li key={file.name} className="flex items-center gap-3">
                <span className="font-medium">{file.name}</span>
                <span className="text-muted">{formatBytes(file.size)}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  aria-label={inferenceMessages.drop.removeFile(file.name)}
                  onClick={() => {
                    onChange(files.filter((f) => f !== file));
                  }}
                >
                  {inferenceMessages.drop.remove}
                </Button>
              </li>
            ))}
          </ul>
          <p className="text-xs text-muted">
            {inferenceMessages.drop.count(files.length, formatBytes(total))}
          </p>
        </>
      )}
    </div>
  );
}
