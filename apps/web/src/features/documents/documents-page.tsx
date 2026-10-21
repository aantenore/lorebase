import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useState, type FormEvent } from 'react';
import { describeError } from '@/api';
import type { DocumentInfo, DocumentUploaded } from '@/api/extraction-types';
import { useKeeperApi } from '@/api/provider';
import { useDocument, useDocuments } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { FileDrop } from '@/features/inference/file-drop';
import { formatBytes } from '@/features/inference/files';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Badge, EmptyState, PageHeader, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { useToast } from '@/ui/toast';
import { JobBadge } from './job-status';

const TIER_LABEL: Record<number, string> = {
  0: 'Rules',
  1: 'Local models',
  2: 'Chat model',
};

interface Outcome {
  name: string;
  /** What happened, in a sentence. */
  text: string;
  failed: boolean;
}

const WORDING = {
  zoneLabel: 'Drop zone for documents',
  prompt: 'Drop documents here, or',
  inputLabel: 'Upload documents',
  note: 'Each document is stored once, whatever its name.',
};

function outcomeOf(name: string, uploaded: DocumentUploaded): Outcome {
  if (uploaded.duplicate) {
    return { name, text: 'Stored before; nothing new was queued.', failed: false };
  }
  return {
    name,
    text:
      uploaded.job_id === null
        ? 'Stored without extraction.'
        : `Stored; extraction job ${uploaded.job_id.toString()} queued.`,
    failed: false,
  };
}

/** Options shared by file and URL intake: whether to extract and with which tiers. */
function ExtractionOptions({
  extract,
  tiers,
  onExtract,
  onTiers,
}: {
  extract: boolean;
  tiers: number[];
  onExtract: (value: boolean) => void;
  onTiers: (value: number[]) => void;
}) {
  return (
    <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <legend className="sr-only">Extraction</legend>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={extract}
          onChange={(event) => {
            onExtract(event.target.checked);
          }}
        />
        Extract after storing
      </label>
      {[0, 1, 2].map((tier) => (
        <label key={tier} className="flex items-center gap-2">
          <input
            type="checkbox"
            disabled={!extract}
            checked={tiers.includes(tier)}
            onChange={(event) => {
              onTiers(
                event.target.checked
                  ? [...tiers, tier].sort((a, b) => a - b)
                  : tiers.filter((t) => t !== tier),
              );
            }}
          />
          Tier {tier} ({TIER_LABEL[tier]})
        </label>
      ))}
    </fieldset>
  );
}

function Intake({ domain }: { domain: string }) {
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { documents } = useConfig();
  const [files, setFiles] = useState<File[]>([]);
  const [extract, setExtract] = useState(documents.extractOnUpload);
  const [tiers, setTiers] = useState(documents.defaultTiers);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [url, setUrl] = useState('');
  const [filename, setFilename] = useState('');

  const options = { extract, ...(extract ? { tiers } : {}) };
  const tiersMissing = extract && tiers.length === 0;

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['keeper', 'documents'] });

  const upload = useMutation({
    mutationFn: async (batch: File[]): Promise<Outcome[]> => {
      const results: Outcome[] = [];
      for (const file of batch) {
        try {
          results.push(
            outcomeOf(file.name, await api.documents.upload(domain, { file, ...options })),
          );
        } catch (error) {
          results.push({ name: file.name, text: describeError(error), failed: true });
        }
      }
      return results;
    },
    meta: { inlineError: true },
    onSuccess: async (results, batch) => {
      setOutcomes(results);
      // Keep what was refused so it can be fixed or removed; drop what was stored.
      const refused = new Set(results.filter((r) => r.failed).map((r) => r.name));
      setFiles(batch.filter((file) => refused.has(file.name)));
      const stored = results.filter((r) => !r.failed).length;
      if (stored > 0) toast.notify(`${stored.toString()} document(s) stored.`, 'success');
      await refresh();
    },
  });

  const fetchUrl = useMutation({
    mutationFn: () =>
      api.documents.upload(domain, {
        url: url.trim(),
        ...(filename.trim() === '' ? {} : { filename: filename.trim() }),
        ...options,
      }),
    meta: { inlineError: true },
    onSuccess: async (uploaded) => {
      setOutcomes([outcomeOf(uploaded.document.filename, uploaded)]);
      setUrl('');
      setFilename('');
      await refresh();
    },
    onError: (error) => {
      setOutcomes([{ name: url.trim(), text: describeError(error), failed: true }]);
    },
  });

  const submitUrl = (event: FormEvent) => {
    event.preventDefault();
    fetchUrl.mutate();
  };

  return (
    <Section title="Add documents">
      <div className="flex flex-col gap-3">
        <FileDrop
          files={files}
          caps={{
            maxFiles: documents.maxFiles,
            maxFileBytes: documents.maxFileBytes,
            maxTotalBytes: Number.POSITIVE_INFINITY,
            fileSuffixes: documents.fileSuffixes,
          }}
          wording={WORDING}
          onChange={setFiles}
        />
        <ExtractionOptions
          extract={extract}
          tiers={tiers}
          onExtract={setExtract}
          onTiers={setTiers}
        />
        {tiersMissing && (
          <p role="alert" className="text-sm text-danger">
            Pick at least one tier, or turn extraction off.
          </p>
        )}
        <div>
          <Button
            variant="primary"
            disabled={files.length === 0 || tiersMissing || upload.isPending}
            onClick={() => {
              upload.mutate(files);
            }}
          >
            {upload.isPending
              ? 'Uploading...'
              : `Upload ${files.length.toString()} ${files.length === 1 ? 'document' : 'documents'}`}
          </Button>
        </div>
        {documents.urlIntake && (
          <form
            aria-label="Add a document by URL"
            onSubmit={submitUrl}
            className="flex flex-wrap items-end gap-2"
          >
            <Input
              label="Document URL"
              type="url"
              className="w-96 max-w-full"
              placeholder="https://"
              value={url}
              hint="Keeper fetches it, from the hosts it allows only."
              onChange={(event) => {
                setUrl(event.target.value);
              }}
            />
            <Input
              label="File name (optional)"
              value={filename}
              onChange={(event) => {
                setFilename(event.target.value);
              }}
            />
            <Button
              type="submit"
              disabled={url.trim() === '' || tiersMissing || fetchUrl.isPending}
            >
              Fetch document
            </Button>
          </form>
        )}
        {outcomes.length > 0 && (
          <ul aria-label="Upload results" className="flex flex-col gap-1 text-sm">
            {outcomes.map((outcome) => (
              <li
                key={outcome.name}
                role={outcome.failed ? 'alert' : undefined}
                className={outcome.failed ? 'text-danger' : undefined}
              >
                <span className="font-medium">{outcome.name}</span>: {outcome.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  );
}

function LibraryRow({ domain, document }: { domain: string; document: DocumentInfo }) {
  const scopeSearch = useScopeSearch();
  const detail = useDocument(domain, document.id);
  const job = detail.data?.extraction_jobs[0];
  return (
    <TR>
      <TD>
        <Link
          to="/$domain/documents/$document"
          params={{ domain, document: document.id }}
          search={scopeSearch}
          className="text-accent hover:underline"
        >
          {document.filename}
        </Link>
      </TD>
      <TD className="text-muted">{document.media_type}</TD>
      <TD>{formatBytes(document.size)}</TD>
      <TD>{shortTime(document.created_at)}</TD>
      <TD>{document.created_by}</TD>
      <TD>{detail.isPending ? <span className="text-muted">...</span> : <JobBadge job={job} />}</TD>
      <TD>
        {job?.proposal_id != null && (
          <Link
            to="/$domain/review/$proposal"
            params={{ domain, proposal: job.proposal_id }}
            search={scopeSearch}
            className="text-accent hover:underline"
          >
            Proposal #{job.proposal_id}
          </Link>
        )}
      </TD>
    </TR>
  );
}

/**
 * Documents (design 12.3, "Documents and extraction jobs"): add documents by drag and drop or
 * URL, see what became of each extraction job, open the Proposal it produced and read the
 * parsed text with the evidence highlighted.
 */
export function DocumentsPage() {
  const scope = useScope();
  const documents = useDocuments(scope.domain);

  return (
    <>
      <PageHeader
        title="Documents"
        description={`Contracts, notices and reports that Lorebase reads into Proposals for ${scope.domainInfo.display_name}.`}
        actions={<Badge tone="accent">Branch {scope.branch}</Badge>}
      />
      <Intake domain={scope.domain} />
      <Section title="Library">
        {documents.isPending ? (
          <p role="status" className="text-muted">
            Loading...
          </p>
        ) : documents.isError ? (
          <p role="alert" className="text-danger">
            {documents.error.message}
          </p>
        ) : documents.data.pages.every((page) => page.items.length === 0) ? (
          <EmptyState>No documents yet. Drop one above.</EmptyState>
        ) : (
          <>
            <Table caption="Documents">
              <THead>
                <TR>
                  <TH>Document</TH>
                  <TH>Type</TH>
                  <TH>Size</TH>
                  <TH>Added</TH>
                  <TH>By</TH>
                  <TH>Extraction</TH>
                  <TH>Proposal</TH>
                </TR>
              </THead>
              <TBody>
                {documents.data.pages
                  .flatMap((page) => page.items)
                  .map((document) => (
                    <LibraryRow key={document.id} domain={scope.domain} document={document} />
                  ))}
              </TBody>
            </Table>
            {documents.hasNextPage && (
              <div className="mt-3">
                <Button
                  disabled={documents.isFetchingNextPage}
                  onClick={() => {
                    void documents.fetchNextPage();
                  }}
                >
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </Section>
    </>
  );
}
