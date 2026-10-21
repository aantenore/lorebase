import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { isApiError } from '@/api';
import type { InferRequest, SchemaProposalDraft } from '@/api/inference-types';
import { useKeeperApi } from '@/api/provider';
import type { Schema } from '@/api/types';
import { useScope } from '@/app/scope';
import { ReviewStep } from './review-step';
import { SourceStep } from './source-step';

/**
 * "Start from data" (design 9.1, 12.3): infer a schema from CSV files or a Postgres source,
 * review and edit the proposal, and apply it as a schema change on a Branch. A protected
 * Branch takes it through a new Branch and a Proposal, the same path as the schema editor.
 */
export function StartFromData({ base }: { base: Schema }) {
  const api = useKeeperApi();
  const scope = useScope();
  const [proposal, setProposal] = useState<SchemaProposalDraft | null>(null);

  const infer = useMutation({
    mutationFn: (request: InferRequest) => api.inference.infer(scope.domain, request),
    onSuccess: setProposal,
  });

  if (proposal !== null) {
    return (
      <ReviewStep
        // A new proposal starts a new review.
        key={proposal.yaml}
        proposal={proposal}
        base={base}
        onRestart={() => {
          setProposal(null);
          infer.reset();
        }}
      />
    );
  }
  return (
    <SourceStep
      pending={infer.isPending}
      error={infer.isError ? describe(infer.error) : null}
      onRun={(request) => {
        infer.mutate(request);
      }}
    />
  );
}

function describe(error: Error): string {
  return isApiError(error) ? error.message : 'The inference request failed.';
}
