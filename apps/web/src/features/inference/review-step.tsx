import { lazy, Suspense, useMemo, useState } from 'react';
import type { SchemaProposalDraft } from '@/api/inference-types';
import type { Schema } from '@/api/types';
import { useConfig } from '@/config/context';
import { ChangeSetPanel } from '@/features/modeler/change-set-panel';
import { schemaToDocument } from '@/features/modeler/document';
import { useChangeCheck } from '@/features/modeler/use-change-check';
import { Button } from '@/ui/button';
import { Section } from '@/ui/layout';
import { DraftProvider } from './draft-context';
import { IssuesList } from './issues-list';
import { ProposalToolbar } from './proposal-toolbar';
import { ProposalTree } from './proposal-tree';
import { useProposalDraft } from './use-proposal-draft';
import { common as commonMessages } from '@/messages/common';
import { inference as inferenceMessages } from '@/messages/inference';

// CodeMirror loads when the preview first shows, as in the schema editor.
const YamlEditor = lazy(() => import('@/features/modeler/yaml-editor'));

const APPLY_LABELS = inferenceMessages.review.applyLabels;
const MESSAGE = inferenceMessages.review.defaultMessage;

export interface ReviewStepProps {
  proposal: SchemaProposalDraft;
  /** Schema of the current Branch, which the accepted elements are laid over. */
  base: Schema;
  onRestart: () => void;
}

/** Review of an inferred proposal: tree, bulk acceptance, YAML preview, issues and apply. */
export function ReviewStep({ proposal, base, onRestart }: ReviewStepProps) {
  const config = useConfig();
  const [percent, setPercent] = useState(Math.round(config.inference.acceptThreshold * 100));
  const draft = useProposalDraft(proposal, base);
  const document = useMemo(() => schemaToDocument(draft.next), [draft.next]);
  const {
    check,
    remote,
    blocked: checkBlocked,
  } = useChangeCheck(draft.changes, document, draft.issues.length === 0);

  const blocked = draft.issues.length > 0 ? inferenceMessages.review.blocked : checkBlocked;
  const problems = [...draft.issues, ...remote];

  return (
    <DraftProvider value={{ draft, threshold: percent / 100 }}>
      <div className="flex flex-col gap-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted">{inferenceMessages.review.notSaved}</p>
          <Button onClick={onRestart}>{inferenceMessages.review.startOver}</Button>
        </div>

        <ProposalToolbar percent={percent} onPercent={setPercent} tables={proposal.tables} />

        <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
          <Section title={inferenceMessages.review.proposedSchema} className="mb-0 min-w-0">
            <ProposalTree />
          </Section>
          <Section
            title={inferenceMessages.review.yamlPreview}
            className="mb-0 min-w-0 xl:sticky xl:top-4"
          >
            <Suspense
              fallback={
                <p role="status" className="text-muted">
                  {commonMessages.loading}
                </p>
              }
            >
              <YamlEditor
                value={draft.yaml}
                onChange={() => undefined}
                label={inferenceMessages.review.yamlLabel}
                jump={null}
                readOnly
                className="h-[32rem]"
              />
            </Suspense>
          </Section>
        </div>

        <IssuesList
          issues={proposal.issues}
          issueCount={proposal.issue_count}
          validation={problems}
        />

        <ChangeSetPanel
          base={base}
          next={draft.next}
          changes={draft.changes}
          document={document}
          blocked={blocked}
          check={{ pending: check.isFetching, result: check.data }}
          defaultMessage={MESSAGE}
          actionLabels={APPLY_LABELS}
        />
      </div>
    </DraftProvider>
  );
}
