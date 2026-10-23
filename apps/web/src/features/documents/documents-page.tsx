import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { useState, type FormEvent } from 'react';
import { describeError } from '@/api';
import type { DocumentInfo, DocumentUploaded } from '@/api/extraction-types';
import { useKeeperApi } from '@/api/provider';
import { useDocument, useDocuments } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { useConfig } from '@/config/context';
import { common as commonMessages } from '@/messages/common';
import { documents as documentsMessages } from '@/messages/documents';
import { FileDrop } from '@/features/inference/file-drop';
import { formatBytes } from '@/features/inference/files';
import { Button } from '@/ui/button';
import { Input } from '@/ui/field';
import { Badge, EmptyState, FormAction, FormRow, PageHeader, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { useToast } from '@/ui/toast';
import { JobBadge } from './job-status';

interface Outcome {
  name: string;
  /** What happened, in a sentence. */
  text: string;
  failed: boolean;
}

const WORDING = {
  zoneLabel: documentsMessages.intake.zoneLabel,
  prompt: documentsMessages.intake.prompt,
  inputLabel: documentsMessages.intake.inputLabel,
  note: documentsMessages.intake.note,
};

function outcomeOf(name: string, uploaded: DocumentUploaded): Outcome {
  if (uploaded.duplicate) {
    return { name, text: documentsMessages.intake.storedBefore, failed: false };
  }
  return {
    name,
    text:
      uploaded.job_id === null
        ? documentsMessages.intake.storedPlain
        : documentsMessages.intake.storedQueued(uploaded.job_id),
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
      <legend className="sr-only">{documentsMessages.intake.extraction}</legend>
      <label className="flex items-center gap-2">
        <input
          type="checkbox"
          checked={extract}
          onChange={(event) => {
            onExtract(event.target.checked);
          }}
        />
        {documentsMessages.intake.extract}
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
          {documentsMessages.intake.tier(tier, documentsMessages.intake.tierNames[tier])}
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
      if (stored > 0) toast.notify(documentsMessages.intake.stored(stored), 'success');
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
    <Section title={documentsMessages.intake.title}>
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
            {documentsMessages.intake.tiersMissing}
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
              ? documentsMessages.intake.uploading
              : documentsMessages.intake.upload(files.length)}
          </Button>
        </div>
        {documents.urlIntake && (
          <form aria-label={documentsMessages.intake.urlForm} onSubmit={submitUrl}>
            <FormRow>
              <Input
                label={documentsMessages.intake.urlLabel}
                type="url"
                className="w-96 max-w-full"
                placeholder="https://"
                value={url}
                hint={documentsMessages.intake.urlHint}
                onChange={(event) => {
                  setUrl(event.target.value);
                }}
              />
              <Input
                label={documentsMessages.intake.filenameLabel}
                value={filename}
                onChange={(event) => {
                  setFilename(event.target.value);
                }}
              />
              <FormAction>
                <Button
                  type="submit"
                  disabled={url.trim() === '' || tiersMissing || fetchUrl.isPending}
                >
                  {documentsMessages.intake.fetch}
                </Button>
              </FormAction>
            </FormRow>
          </form>
        )}
        {outcomes.length > 0 && (
          <ul aria-label={documentsMessages.intake.results} className="flex flex-col gap-1 text-sm">
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
      <TD>
        {detail.isPending ? (
          <span className="text-muted">{documentsMessages.library.pending}</span>
        ) : (
          <JobBadge job={job} />
        )}
      </TD>
      <TD>
        {job?.proposal_id != null && (
          <Link
            to="/$domain/review/$proposal"
            params={{ domain, proposal: job.proposal_id }}
            search={{ ...scopeSearch, tab: 'items' }}
            className="text-accent hover:underline"
          >
            {documentsMessages.library.proposalLink(job.proposal_id)}
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
        title={documentsMessages.title}
        description={documentsMessages.description}
        docs={documentsMessages.docs}
        actions={<Badge tone="accent">{documentsMessages.branchBadge(scope.branch)}</Badge>}
      />
      <Intake domain={scope.domain} />
      <Section title={documentsMessages.library.title}>
        {documents.isPending ? (
          <p role="status" className="text-muted">
            {commonMessages.loading}
          </p>
        ) : documents.isError ? (
          <p role="alert" className="text-danger">
            {documents.error.message}
          </p>
        ) : documents.data.pages.every((page) => page.items.length === 0) ? (
          <EmptyState>{documentsMessages.library.empty}</EmptyState>
        ) : (
          <>
            <Table caption={documentsMessages.library.caption}>
              <THead>
                <TR>
                  <TH>{documentsMessages.library.document}</TH>
                  <TH>{documentsMessages.library.type}</TH>
                  <TH>{documentsMessages.library.size}</TH>
                  <TH>{documentsMessages.library.added}</TH>
                  <TH>{documentsMessages.library.by}</TH>
                  <TH>{documentsMessages.library.extraction}</TH>
                  <TH>{documentsMessages.library.proposal}</TH>
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
                  {documentsMessages.loadMore}
                </Button>
              </div>
            )}
          </>
        )}
      </Section>
    </>
  );
}
