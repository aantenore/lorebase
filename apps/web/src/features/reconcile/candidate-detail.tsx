import { Link } from '@tanstack/react-router';
import { useMemo } from 'react';
import { useCandidate, useRecord, useSchema } from '@/api/queries';
import type { ReconciliationCandidate, ReconciliationClaim } from '@/api/reconciliation-types';
import type { JsonValue, Kind, Schema } from '@/api/types';
import { parseAttributeType, type ValueSpec } from '@/api/values';
import { RecordLink } from '@/app/record-link';
import { useScope, useScopeSearch } from '@/app/scope';
import { EvidenceLink } from '@/features/extraction/evidence-link';
import { DiffText } from '@/features/review/side-by-side';
import { reconcile as reconcileMessages } from '@/messages/reconcile';
import { Badge, Card, CardGrid, QueryBoundary, Section } from '@/ui/layout';
import { shortTime } from '@/ui/time';
import { DecisionOutcome, ProposalLink, useDecide, type Decide } from './decide';
import { DecisionForm, ReopenForm } from './decision-form';
import { MergeForm } from './merge-form';
import {
  candidateTitle,
  currentClaim,
  decisionReason,
  periodOf,
  showValue,
  signalBadges,
  sourceLabel,
} from './reconcile-state';
import { STATUS_TONE } from './status';

const text = reconcileMessages;

function specOf(kind: Kind | undefined, attribute: string | null): ValueSpec | null {
  const found = Object.values(kind?.attributes ?? {}).find((a) => a.api_name === attribute);
  if (found === undefined || parseAttributeType(found.type) === null) return null;
  return { type: found.type, required: found.required, constraints: found.constraints };
}

function ClaimCard({
  claim,
  index,
  current,
  base,
  sourceNames,
}: {
  claim: ReconciliationClaim;
  index: number;
  current: boolean;
  /** The current value, to mark what a text claim changes in it. */
  base: JsonValue | undefined;
  sourceNames: ReadonlyMap<string, string>;
}) {
  const scope = useScope();
  const scopeSearch = useScopeSearch();
  const period = periodOf(claim);
  const value = showValue(claim.value);
  return (
    <Card as="article" aria-label={text.claim.label(index)} className="gap-2">
      <header className="flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold">{text.claim.label(index)}</h4>
        {current && <Badge tone="success">{text.claim.current}</Badge>}
      </header>
      <p
        aria-label={text.claim.value}
        className="font-mono text-sm break-words whitespace-pre-wrap"
      >
        {!current && typeof base === 'string' && typeof claim.value === 'string' ? (
          <DiffText base={base} value={claim.value} />
        ) : (
          value
        )}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">{text.claim.source}</dt>
        <dd className="break-words">{sourceLabel(claim.source, sourceNames)}</dd>
        <dt className="text-muted">{text.claim.at}</dt>
        <dd>{claim.at === null ? text.claim.none : shortTime(claim.at)}</dd>
        {period !== null && (
          <>
            <dt className="text-muted">{text.claim.period}</dt>
            <dd>{period}</dd>
          </>
        )}
      </dl>
      {claim.evidence !== null && (
        <figure className="flex flex-col gap-1 text-xs">
          <figcaption className="text-muted">{text.claim.evidence}</figcaption>
          <blockquote className="border-l-2 border-line pl-2 break-words whitespace-pre-wrap">
            {claim.evidence.quote}
          </blockquote>
          <EvidenceLink
            item={{ id: claim.proposal_item?.item_id, evidence: claim.evidence }}
            children={text.claim.viewInDocument}
          />
        </figure>
      )}
      {claim.proposal_item !== null && (
        <Link
          to="/$domain/review/$proposal"
          params={{ domain: scope.domain, proposal: claim.proposal_item.proposal_id }}
          search={{ ...scopeSearch, tab: 'items', item: claim.proposal_item.item_id }}
          className="text-xs text-accent hover:underline"
        >
          {text.claim.proposalItem(claim.proposal_item.proposal_id)}
        </Link>
      )}
    </Card>
  );
}

/** Claims of a value candidate side by side; the claim holding the current value is marked. */
function Claims({
  decide,
  candidate,
  kind,
  sourceNames,
}: {
  decide: Decide;
  candidate: ReconciliationCandidate;
  kind: string;
  sourceNames: ReadonlyMap<string, string>;
}) {
  const scope = useScope();
  const record = useRecord(scope, kind, candidate.keys[0] ?? '');
  const current =
    candidate.attribute === null
      ? undefined
      : (record.data?.record.attributes[candidate.attribute] as JsonValue | undefined);
  const holder = currentClaim(candidate, current);
  return (
    <>
      <Section
        title={text.detail.claimsTitle}
        {...(candidate.claim_count > candidate.claims.length
          ? { description: text.detail.claimsMore(candidate.claims.length, candidate.claim_count) }
          : {})}
      >
        <CardGrid minWidth="15rem" label={text.detail.claimsTitle}>
          {candidate.claims.map((claim, index) => (
            <div role="listitem" key={claim.claim_id} className="min-w-0">
              <ClaimCard
                claim={claim}
                index={index + 1}
                current={claim.claim_id === holder?.claim_id}
                base={holder?.value}
                sourceNames={sourceNames}
              />
            </div>
          ))}
        </CardGrid>
      </Section>
      {candidate.status === 'open' && (
        <DecisionPanel decide={decide} candidate={candidate} currentClaimId={holder?.claim_id} />
      )}
    </>
  );
}

function DecisionPanel({
  decide,
  candidate,
  currentClaimId,
}: {
  decide: Decide;
  candidate: ReconciliationCandidate;
  currentClaimId: string | undefined;
}) {
  const scope = useScope();
  const schema = useSchema(scope);
  const kind = schema.data?.kinds.find((k) => k.api_name === candidate.kind);
  return (
    <Section title={text.decide.title}>
      <DecisionForm
        decide={decide}
        candidate={candidate}
        spec={specOf(kind, candidate.attribute)}
        sourceNames={sourceNamesOf(schema.data)}
        currentClaimId={currentClaimId}
      />
    </Section>
  );
}

const sourceNamesOf = (schema: Schema | undefined): ReadonlyMap<string, string> =>
  new Map((schema?.sources ?? []).map((s) => [s.id, s.api_name]));

function Body({ candidate }: { candidate: ReconciliationCandidate }) {
  const scope = useScope();
  const schema = useSchema(scope);
  // The decision outlives the form that made it: a dismissal swaps the decision form for the
  // reopen form, and its outcome stays in view.
  const decide = useDecide(scope.domain, candidate.id);
  const sourceNames = useMemo(() => sourceNamesOf(schema.data), [schema.data]);
  const kind = schema.data?.kinds.find((k) => k.api_name === candidate.kind);
  const title = candidateTitle(candidate);
  const reason = decisionReason(candidate);
  const badges = signalBadges(candidate.signals);
  return (
    <section
      aria-label={text.detail.label(title)}
      className="flex min-w-0 flex-col gap-4 rounded-md border border-line bg-surface p-4"
    >
      <header className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="danger">{text.typeOne[candidate.type]}</Badge>
          <Badge tone={STATUS_TONE[candidate.status]}>{text.statuses[candidate.status]}</Badge>
          <h3 className="text-sm font-semibold break-words">{title}</h3>
        </div>
        <p className="flex flex-wrap gap-x-3 text-sm">
          {candidate.kind !== null &&
            candidate.keys.map((key) => (
              <RecordLink
                key={key}
                domain={scope.domain}
                recordRef={`${candidate.kind ?? ''}/${key}`}
              />
            ))}
        </p>
        <p className="text-xs text-muted">
          {text.detail.created(shortTime(candidate.created_at))}
          {candidate.decided_by !== null &&
            candidate.decided_at !== null &&
            ` · ${text.detail.decided(candidate.decided_by, shortTime(candidate.decided_at))}`}
        </p>
        {reason !== null && <p className="text-sm">{text.detail.reason(reason)}</p>}
        {candidate.proposal_id !== null && (
          <p className="text-sm">
            <span className="text-muted">{text.detail.linkedProposal}: </span>
            <ProposalLink domain={scope.domain} proposal={candidate.proposal_id} />
          </p>
        )}
      </header>

      {badges.length > 0 && (
        <ul aria-label={text.signals.label} className="flex flex-wrap gap-1">
          {badges.map((badge) => (
            <li key={badge.key} title={badge.hint}>
              <Badge tone={badge.tone}>{badge.label}</Badge>
            </li>
          ))}
        </ul>
      )}

      {candidate.kind === null || kind === undefined ? (
        schema.data !== undefined && <p className="text-sm text-muted">{text.detail.noKind}</p>
      ) : candidate.type === 'duplicate' ? (
        candidate.status === 'open' && (
          <Section title={text.merge.title}>
            <MergeForm decide={decide} candidate={candidate} kind={kind} />
          </Section>
        )
      ) : (
        <Claims
          decide={decide}
          candidate={candidate}
          kind={kind.api_name}
          sourceNames={sourceNames}
        />
      )}

      {candidate.status === 'dismissed' && (
        <Section title={text.decide.reopenTitle}>
          <ReopenForm decide={decide} />
        </Section>
      )}

      <DecisionOutcome
        domain={scope.domain}
        candidate={candidate}
        decision={decide.data}
        error={decide.error}
      />
    </section>
  );
}

/** One candidate: its claims with source, time, period and evidence, and the decision on it. */
export function CandidateDetail({ id }: { id: string }) {
  const scope = useScope();
  const candidate = useCandidate(scope.domain, id);
  return <QueryBoundary query={candidate}>{(data) => <Body candidate={data} />}</QueryBoundary>;
}
