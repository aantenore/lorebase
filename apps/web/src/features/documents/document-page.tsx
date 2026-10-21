import { Link, useNavigate } from '@tanstack/react-router';
import type { DocumentDetail, ExtractionItem } from '@/api/extraction-types';
import { useDocument, useDocumentText, useProposalItems } from '@/api/queries';
import { useScope, useScopeSearch } from '@/app/scope';
import { targetOf } from '@/features/extraction/item-state';
import { formatBytes } from '@/features/inference/files';
import { Badge, EmptyState, PageHeader, QueryBoundary, Section } from '@/ui/layout';
import { Table, TBody, TD, TH, THead, TR } from '@/ui/table';
import { shortTime } from '@/ui/time';
import { DocumentText } from './document-text';
import { spansOf } from './document-spans';
import { costOf, JobBadge, jobError, tokensOf } from './job-status';
import { cn } from '@/ui/cn';

const DECISION_TONE = { pending: 'warning', accepted: 'success', rejected: 'neutral' } as const;

/** What an item proposes, in a few words. */
export function itemLabel(item: ExtractionItem): string {
  if (item.type === 'link')
    return `${item.relation ?? ''} ${item.from ?? '?'} -> ${item.to ?? '?'}`;
  if (item.type === 'value') {
    return `${item.kind ?? ''}/${item.key ?? ''} ${item.attribute ?? ''} = ${JSON.stringify(item.value)}`;
  }
  return `${targetOf(item)} ${item.key ?? ''}`;
}

function Jobs({ detail, domain }: { detail: DocumentDetail; domain: string }) {
  const scopeSearch = useScopeSearch();
  if (detail.extraction_jobs.length === 0) {
    return <EmptyState>No extraction job was queued for this document.</EmptyState>;
  }
  return (
    <Table caption="Extraction jobs">
      <THead>
        <TR>
          <TH>Job</TH>
          <TH>Status</TH>
          <TH>Model</TH>
          <TH>Prompt</TH>
          <TH>Started</TH>
          <TH>Finished</TH>
          <TH>Tokens</TH>
          <TH>Cost</TH>
          <TH>Proposal</TH>
        </TR>
      </THead>
      <TBody>
        {detail.extraction_jobs.map((job) => (
          <TR key={job.id}>
            <TD className="font-mono text-xs">{job.id}</TD>
            <TD>
              <JobBadge job={job} />
              {job.status === 'failed' && (
                <p className="mt-1 max-w-xs text-xs text-danger">{jobError(job)}</p>
              )}
            </TD>
            <TD>{job.model}</TD>
            <TD>{job.prompt_version}</TD>
            <TD>{shortTime(job.created_at)}</TD>
            <TD>{job.finished_at === null ? '' : shortTime(job.finished_at)}</TD>
            <TD>{tokensOf(job)?.toLocaleString('en') ?? ''}</TD>
            <TD>{costOf(job) ?? ''}</TD>
            <TD>
              {job.proposal_id !== null && (
                <Link
                  to="/$domain/review/$proposal"
                  params={{ domain, proposal: job.proposal_id }}
                  search={{ ...scopeSearch, tab: 'items' }}
                  className="text-accent hover:underline"
                >
                  #{job.proposal_id}
                </Link>
              )}
            </TD>
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

function EvidenceList({
  items,
  active,
  proposal,
  onSelect,
}: {
  items: ExtractionItem[];
  active: string | null;
  proposal: string;
  onSelect: (item: ExtractionItem) => void;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  return (
    <ul aria-label="Items quoting this document" className="flex flex-col gap-2">
      {items.map((item) => (
        <li
          key={item.id}
          className={cn(
            'rounded-md border bg-surface p-2 text-sm',
            item.id === active ? 'border-accent' : 'border-line',
          )}
        >
          <button
            type="button"
            aria-current={item.id === active ? 'true' : undefined}
            className="w-full text-left"
            onClick={() => {
              onSelect(item);
            }}
          >
            <span className="flex flex-wrap items-center gap-2">
              <Badge tone={DECISION_TONE[item.decision]}>{item.decision}</Badge>
              <span className="font-mono text-xs break-all">{itemLabel(item)}</span>
            </span>
            <span className="mt-1 block text-xs text-muted">{item.evidence?.quote}</span>
          </button>
          <Link
            to="/$domain/review/$proposal"
            params={{ domain: scope.domain, proposal }}
            search={{ ...scopeSearch, tab: 'items', item: item.id }}
            className="mt-1 inline-block text-xs text-accent hover:underline"
          >
            Review this item
          </Link>
        </li>
      ))}
    </ul>
  );
}

/** Passages of the evidence, for a Keeper that does not serve the parsed text. */
function Passages({ items, active }: { items: ExtractionItem[]; active: string | null }) {
  const sorted = [...items].sort((a, b) => (a.evidence?.start ?? 0) - (b.evidence?.start ?? 0));
  return (
    <ol aria-label="Evidence passages" className="flex flex-col gap-2">
      {sorted.map((item) => (
        <li
          key={item.id}
          className={cn(
            'rounded-md border bg-surface p-2 text-sm',
            item.id === active ? 'border-accent' : 'border-line',
          )}
        >
          <p className="text-xs text-muted">
            Characters {item.evidence?.start.toString()} to {item.evidence?.end.toString()}
          </p>
          <blockquote className="whitespace-pre-wrap">{item.evidence?.quote}</blockquote>
        </li>
      ))}
    </ol>
  );
}

function Viewer({
  domain,
  document,
  detail,
  item,
  start,
  end,
  onSelect,
}: {
  domain: string;
  document: string;
  detail: DocumentDetail;
  item: string | null;
  start: number | undefined;
  end: number | undefined;
  onSelect: (item: ExtractionItem | null, proposal: string) => void;
}) {
  const text = useDocumentText(domain, document, detail.text_sha256);
  const proposal = detail.extraction_jobs.find((job) => job.proposal_id !== null)?.proposal_id;
  const items = useProposalItems(domain, proposal ?? '', proposal != null);
  const mine = (items.data ?? []).filter((i) => i.evidence?.document_id === document);
  // A link may name the passage by its offsets instead of by item.
  const active =
    item ??
    mine.find((i) => i.evidence?.start === start && i.evidence?.end === end && start !== undefined)
      ?.id ??
    null;

  if (text.isPending || (proposal != null && items.isPending)) {
    return (
      <p role="status" className="text-muted">
        Loading...
      </p>
    );
  }
  if (text.isError) {
    return (
      <p role="alert" className="text-danger">
        {text.error.message}
      </p>
    );
  }
  const body = text.data;
  if (body === null && mine.length === 0) {
    return <EmptyState>The text of this document has not been parsed yet.</EmptyState>;
  }
  // Keeper's offsets count code points, not UTF-16 units.
  const shown = body === null ? 0 : Array.from(body.text).length;
  return (
    <div className="grid items-start gap-4 lg:grid-cols-[1fr_minmax(16rem,24rem)]">
      <div>
        {body === null ? (
          <>
            <p className="mb-2 text-sm text-muted">
              The parsed text is not available yet; the passages the items quote are shown.
            </p>
            <Passages items={mine} active={active} />
          </>
        ) : (
          <>
            {body.truncated && (
              <p className="mb-2 text-sm text-muted">
                The text is long: the first {shown.toLocaleString()} of{' '}
                {body.length.toLocaleString()} characters are shown.
              </p>
            )}
            <DocumentText
              text={body.text}
              spans={spansOf(mine, document, shown)}
              active={active}
              onSelect={(id) => {
                onSelect(mine.find((item) => item.id === id) ?? null, proposal ?? '');
              }}
            />
          </>
        )}
      </div>
      {proposal != null && mine.length > 0 && (
        <EvidenceList
          items={mine}
          active={active}
          proposal={proposal}
          onSelect={(item) => {
            onSelect(item, proposal);
          }}
        />
      )}
    </div>
  );
}

/**
 * One document: its jobs, and its parsed text with the evidence of the items highlighted.
 * `item` (or `start` and `end`) in the URL selects the passage to show, so a link from the
 * item review lands on the quote.
 */
export function DocumentPage({
  documentId,
  item = null,
  start,
  end,
}: {
  documentId: string;
  /** The item whose evidence is selected. */
  item?: string | null;
  start?: number | undefined;
  end?: number | undefined;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const navigate = useNavigate();
  const detail = useDocument(scope.domain, documentId);

  return (
    <QueryBoundary query={detail}>
      {(data) => (
        <>
          <PageHeader
            title={data.filename}
            description={`${data.media_type}, ${formatBytes(data.size)}, added ${shortTime(data.created_at)} by ${data.created_by}.`}
            actions={
              <Link
                to="/$domain/documents"
                params={{ domain: scope.domain }}
                search={scopeSearch}
                className="text-sm text-accent hover:underline"
              >
                Back to the library
              </Link>
            }
          />
          <Section title="Extraction jobs">
            <Jobs detail={data} domain={scope.domain} />
          </Section>
          <Section title="Parsed text">
            <Viewer
              domain={scope.domain}
              document={documentId}
              detail={data}
              item={item}
              start={start}
              end={end}
              onSelect={(selected) => {
                void navigate({
                  to: '.',
                  search: (previous) => ({
                    ...previous,
                    item: selected?.id,
                    start: selected?.evidence?.start,
                    end: selected?.evidence?.end,
                  }),
                  replace: true,
                });
              }}
            />
          </Section>
        </>
      )}
    </QueryBoundary>
  );
}
