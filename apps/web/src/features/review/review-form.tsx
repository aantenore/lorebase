import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useKeeperApi } from '@/api/provider';
import type { ProposalDetail, ReviewVerdict } from '@/api/types';
import { Button } from '@/ui/button';
import { Select, Textarea } from '@/ui/field';
import { Badge } from '@/ui/layout';
import { useToast } from '@/ui/toast';

const VERDICTS: { value: ReviewVerdict; label: string }[] = [
  { value: 'approve', label: 'Approve' },
  { value: 'request_changes', label: 'Request changes' },
  { value: 'comment', label: 'Comment only' },
];

/** What the target Branch still needs before it takes the merge, in one line. */
export function approvalSummary(detail: ProposalDetail): string {
  const { approval } = detail;
  if (!approval.protected) return 'The target Branch needs no approval.';
  const parts = [`${approval.approvals} of ${approval.required} approval(s)`];
  if (approval.changes_requested.length > 0) {
    parts.push(`changes requested by ${approval.changes_requested.join(', ')}`);
  }
  return parts.join('; ');
}

/** Reviews of an open Proposal: where the approvals stand, and a form to add one. */
export function ReviewForm({ domain, proposal }: { domain: string; proposal: ProposalDetail }) {
  const api = useKeeperApi();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [verdict, setVerdict] = useState<ReviewVerdict>('approve');
  const [comment, setComment] = useState('');

  const submit = useMutation({
    mutationFn: () =>
      api.proposals.review(domain, proposal.id, {
        verdict,
        ...(comment.trim() === '' ? {} : { comment: comment.trim() }),
      }),
    onSuccess: async () => {
      toast.notify('Review added.', 'success');
      setComment('');
      await queryClient.invalidateQueries({ queryKey: ['keeper'] });
    },
  });

  return (
    <div className="flex flex-col gap-3">
      <p className="flex items-center gap-2 text-sm">
        <Badge tone={proposal.approval.satisfied ? 'success' : 'warning'}>
          {proposal.approval.satisfied ? 'approved' : 'waiting'}
        </Badge>
        <span>{approvalSummary(proposal)}</span>
      </p>
      {proposal.status === 'open' && (
        <form
          aria-label="Add a review"
          className="flex max-w-xl flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            submit.mutate();
          }}
        >
          <div className="w-56">
            <Select
              label="Verdict"
              value={verdict}
              options={VERDICTS}
              onChange={(event) => {
                setVerdict(event.target.value as ReviewVerdict);
              }}
            />
          </div>
          <Textarea
            label="Comment"
            value={comment}
            rows={2}
            onChange={(event) => {
              setComment(event.target.value);
            }}
          />
          <div>
            <Button type="submit" disabled={submit.isPending}>
              Submit review
            </Button>
          </div>
        </form>
      )}
    </div>
  );
}
